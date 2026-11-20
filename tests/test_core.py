import json
from types import SimpleNamespace

import pytest

from conftest import SC
from scripted import OverconfidentModel, ScriptedModel
from finguard_core import guardrails
from finguard_core.config import GuardrailConfig
from finguard_core.runner import run_case
from finguard_core.strands_backend import ProposalModel, TraceHooks
from finguard_core.runner import Recorder


def fev(tid):
    t = next(t for t in json.load(open("data/transactions.json")) if t["txn_id"] == tid)
    return {"type": "flagged_transaction", **{k: t[k] for k in ("txn_id", "customer_id", "ts", "amount", "merchant_id", "device_id", "geo", "rule_triggered")}}


def kev(doc_id):
    d = next(d for d in SC["kyc_docs"] if d["doc_id"] == doc_id); return {"type": "loan_document", "doc_id": doc_id, "customer_id": d["customer_id"]}


def case(aws, audit, agent, event, model=None):
    r = run_case(agent, event, model or ScriptedModel(aws["tools"]), audit); return r, audit.latest(r["case_id"])


# ---------------- guardrails (unit) ----------------
def test_never_auto_block_above_limit():
    v = guardrails.apply("fraud", {"decision": "AUTO_BLOCK", "confidence": .97}, {"amount": 190_000})
    assert v.decision == "ESCALATE" and v.recommend_block and v.triggered[0]["rule"] == "G2_BLOCK_VALUE_LIMIT"

def test_low_confidence_and_invalid_decision():
    assert guardrails.apply("kyc", {"decision": "AUTO_APPROVE", "confidence": .5}, {"amount": 1}).triggered[0]["rule"] == "G1_LOW_CONFIDENCE"
    assert guardrails.apply("kyc", {"decision": "NUKE", "confidence": .99}, {}).triggered[0]["rule"] == "G0_INVALID_DECISION"

def test_contradiction_forces_escalation():
    v = guardrails.apply("fraud", {"decision": "AUTO_CLEAR", "confidence": .99}, {"amount": 10, "contradiction": "evidence says otherwise"})
    assert v.decision == "ESCALATE" and v.triggered[0]["rule"] == "G9_EVIDENCE_CONTRADICTION"

def test_thresholds_configurable():
    assert guardrails.apply("fraud", {"decision": "AUTO_BLOCK", "confidence": .97}, {"amount": 28_000}, GuardrailConfig(fraud_auto_block_max_value=10_000)).decision == "ESCALATE"


# ---------------- tools against moto DynamoDB / S3 ----------------
def test_fraud_tools(aws):
    t = aws["tools"]; sev = fev(SC["fraud"]["block_highvalue"])
    h = t.get_transaction_history(sev["customer_id"], sev["ts"]); assert h["count"] >= 3 and h["mean"] > 0
    g = t.check_device_geo_consistency(sev["customer_id"], sev["device_id"], sev["geo"]["lat"], sev["geo"]["lon"], sev["geo"]["country"], sev["ts"])
    assert g["foreign"] and g["impossible_travel"] and not g["known_device"]
    assert "error" in t.check_merchant_risk("M9999")

def test_kyc_tools_via_textract_parsing(aws):
    t = aws["tools"]
    r = t.extract_document_fields("LD-007"); assert r["warnings"]                       # malformed PAN as read
    r2 = t.extract_document_fields("LD-007", "ocr_strict"); assert not any("fails format" in w for w in r2["warnings"]) and r2["warnings"]   # repaired + noted
    assert t.find_customer_by_pan("BLKPX9999Z")["blacklisted"]

def test_recon_diff(aws):
    r = aws["tools"].diff_ledgers("B-TEST", "ledgers/core/ledger_core.csv", "ledgers/settlement/ledger_settlement.csv")
    assert r["mismatches"] == 13


# ---------------- audit ----------------
def test_audit_query_and_reverse(aws, audit):
    r, rec = case(aws, audit, "fraud", fev(SC["fraud"]["clear_ok"]))
    assert r["decision"] == "AUTO_CLEAR" and rec["tool_calls"] and rec["reasoning_trace"] and rec["undo_instruction"] and rec["model_info"]["model_id"]
    assert [x["case_id"] for x in audit.list_cases(status="AUTO_RESOLVED")] == [r["case_id"]]
    audit.reverse(r["case_id"], "analyst1", "customer called")
    assert audit.get_case(r["case_id"])[-1]["record_type"] == "reversal" and audit.list_cases(status="AUTO_RESOLVED") == []
    with pytest.raises(ValueError): audit.reverse(r["case_id"], "a", "again")

def test_human_owned_not_reversible(aws, audit):
    r, _ = case(aws, audit, "fraud", fev(SC["fraud"]["block_highvalue"]))
    with pytest.raises(ValueError): audit.reverse(r["case_id"], "a", "b")


# ---------------- end-to-end: model -> verification -> guardrails -> audit ----------------
def test_fraud_guardrails(aws, audit):
    r, _ = case(aws, audit, "fraud", fev(SC["fraud"]["block_highvalue"])); assert r["proposed"] == "AUTO_BLOCK" and r["decision"] == "ESCALATE" and r["recommend_block"] and "G2_BLOCK_VALUE_LIMIT" in r["guardrails"]
    r, _ = case(aws, audit, "fraud", fev(SC["fraud"]["clear_but_highvalue"])); assert r["proposed"] == "AUTO_CLEAR" and "G3_CLEAR_VALUE_LIMIT" in r["guardrails"]
    r, _ = case(aws, audit, "fraud", fev(SC["fraud"]["low_confidence"])); assert r["decision"] == "ESCALATE" and "G1_LOW_CONFIDENCE" in r["guardrails"]

def test_overconfident_model_is_caught(aws, audit):
    r, rec = case(aws, audit, "fraud", fev(SC["fraud"]["block_highvalue"]), OverconfidentModel())
    assert r["decision"] == "ESCALATE" and r["requires_human"] and r["tools"] == []
    assert any(g["rule"] in ("G9_EVIDENCE_CONTRADICTION", "G1_LOW_CONFIDENCE") for g in rec["guardrails"]) and rec["confidence"] <= .4      # confidence capped by zero coverage
    r, _ = case(aws, audit, "kyc", kev(SC["kyc"]["blacklisted"]), OverconfidentModel()); assert r["decision"] == "ESCALATE"
    r, _ = case(aws, audit, "recon", {"type": "mismatch", "batch_id": "B-TEST", "mismatch_id": "MM-001", "kind": "x", "ref": "r", "amount": 10}, OverconfidentModel()); assert r["decision"] == "ESCALATE"

def test_kyc_replan_and_outcomes(aws, audit):
    r, rec = case(aws, audit, "kyc", kev(SC["kyc"]["pan_fixable"]))
    assert r["tools"].count("extract_document_fields") == 2 and r["decision"] == "AUTO_APPROVE" and any("malformed" in t.get("thought", "") for t in rec["reasoning_trace"])
    got = {d["scenario"]: run_case("kyc", {"type": "loan_document", "doc_id": d["doc_id"], "customer_id": d["customer_id"]}, ScriptedModel(aws["tools"]), audit)["decision"] for d in SC["kyc_docs"]}
    assert got["blacklisted"] == got["pan_other_customer"] == got["high_value_clean"] == "ESCALATE"
    assert got["clean"] == got["name_typo"] == got["pan_fixable"] == "AUTO_APPROVE"
    assert got["expired_id"] == got["missing_income"] == got["pan_unfixable"] == "FLAG_MISSING_INFO"

def test_recon_batch(aws, audit):
    aws["tools"].diff_ledgers("B-1", "ledgers/core/ledger_core.csv", "ledgers/settlement/ledger_settlement.csv")
    from collections import Counter
    c = Counter(run_case("recon", {"type": "mismatch", "batch_id": "B-1", "mismatch_id": m["mismatch_id"], "kind": m["kind"], "ref": m["ref"], "amount": m["amount"]}, ScriptedModel(aws["tools"]), audit)["decision"] for m in aws["repo"].mismatches("B-1"))
    assert c["AUTO_RESOLVE"] == 5 and c["FLAG_REVERSAL"] == 2 and c["DEFER"] == 3 and c["ESCALATE"] == 3     # 3 escalated = 2 breaks + the INR 31L timing diff (G7)

def test_hold_message_sanitised_by_bedrock_guardrail(aws, audit):
    class FakeBR:
        def apply_guardrail(self, **kw): return {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "Your transaction is on hold; call us. Contact {PHONE}."}]}
    for tid in (SC["fraud"]["hold_medium"],):
        r = run_case("fraud", fev(tid), ScriptedModel(aws["tools"]), audit, bedrock_runtime=FakeBR(), guardrail_id="gr-1", guardrail_version="1")
        if r["decision"] == "HOLD_AND_NOTIFY":
            rec = audit.latest(r["case_id"]); assert "{PHONE}" in rec["summary"] and "9876543210" not in rec["summary"]

def test_agent_crash_never_auto_resolves(aws, audit):
    class Boom:
        def investigate(self, *a): raise RuntimeError("bedrock throttled")
    r, rec = case(aws, audit, "fraud", fev(SC["fraud"]["clear_ok"]), Boom()); assert r["decision"] == "ESCALATE" and r["requires_human"]


# ---------------- Strands integration surface (no network) ----------------
def test_trace_hooks_capture_tool_calls():
    rec = Recorder(); h = TraceHooks(rec)
    h.on_message(SimpleNamespace(message={"role": "assistant", "content": [{"text": "check merchant"}, {"toolUse": {"name": "fraud___check_merchant_risk", "input": {"merchant_id": "M1"}}}]}))
    h.on_tool(SimpleNamespace(tool_use={"name": "fraud___check_merchant_risk", "input": {"merchant_id": "M1"}}, result={"status": "success", "content": [{"text": json.dumps({"risk_score": 80})}]}))
    assert rec.calls[0]["tool"] == "check_merchant_risk" and rec.calls[0]["result"]["risk_score"] == 80 and rec.trace[0]["next"] == ["check_merchant_risk"]

def test_strands_agent_constructs_offline():
    from strands import Agent
    from strands.models.bedrock import BedrockModel
    a = Agent(model=BedrockModel(model_id="anthropic.claude-sonnet-4-5-20250929-v1:0", region_name="ap-south-1", temperature=0, guardrail_id="g", guardrail_version="1"), tools=[], hooks=[TraceHooks(Recorder())], callback_handler=None)
    assert a and ProposalModel(decision="AUTO_CLEAR", confidence=.9, reasoning=["x"]).confidence == .9
