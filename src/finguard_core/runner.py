"""Runs one case: backend investigates (LLM agent) -> code verifies -> guardrails -> audit log.
The backend is pluggable: StrandsBackend (Bedrock + AgentCore Gateway) in AWS, a scripted backend in tests."""
from dataclasses import dataclass, field

from . import guardrails, verify
from .config import DEFAULT_CFG
from .guardrails import ALLOWED


@dataclass
class Proposal:
    decision: str
    confidence: float
    reasoning: list = field(default_factory=list)
    risk_score: int | None = None
    customer_message: str | None = None


class Recorder:
    """Captures the plan/act/observe trace and every tool call, whatever executes the tools."""
    def __init__(self): self.calls, self.trace, self.step = [], [], 0

    def plan(self, thought, nxt=None):
        self.step += 1; self.trace.append({"step": self.step, "type": "plan", "thought": thought, "next": nxt or []})

    def call(self, tool, args, result):
        self.calls.append({"step": self.step, "tool": tool, "args": args, "result": result})
        note = f"{tool} FAILED: {result['error']}" if "error" in result else f"{tool} -> {str(result)[:240]}"
        self.trace.append({"step": self.step, "type": "observe", "note": note})


CASE_ID = {"fraud": lambda e: f"FRD-{e['txn_id']}", "kyc": lambda e: f"KYC-{e['doc_id']}", "recon": lambda e: f"REC-{e['batch_id']}-{e['mismatch_id']}"}


def stake(agent, event, ev):
    if agent == "kyc": return ev.get("extract_document_fields", {}).get("fields", {}).get("loan_amount", 0)
    return event.get("amount", 0)


def undo_instruction(agent, decision, event):
    return {"fraud": {"AUTO_CLEAR": f"Re-flag txn {event.get('txn_id')} and send to the fraud analyst queue.",
                      "HOLD_AND_NOTIFY": f"Release the hold on txn {event.get('txn_id')} and send the customer an all-clear.",
                      "AUTO_BLOCK": f"Unblock customer {event.get('customer_id')} and restore card/account access."},
            "kyc": {"AUTO_APPROVE": f"Remove {event.get('doc_id')} from the underwriter queue and return it to KYC review."},
            "recon": {"AUTO_RESOLVE": f"Re-open mismatch {event.get('mismatch_id')} and return it to the break queue.",
                      "DEFER": f"Remove deferral on {event.get('mismatch_id')}; re-evaluate in today's batch.",
                      "FLAG_REVERSAL": f"Withdraw reversal flag for {event.get('mismatch_id')}; entry stays posted."}}[agent].get(decision)


def sanitize_customer_message(client, guardrail_id, version, text):
    """Bedrock Guardrails ApplyGuardrail (OUTPUT) on anything shown to a customer: PII anonymised, denied topics blocked."""
    if not (client and guardrail_id and text): return text, None
    r = client.apply_guardrail(guardrailIdentifier=guardrail_id, guardrailVersion=version, source="OUTPUT", content=[{"text": {"text": text}}])
    out = "".join(o["text"] for o in r.get("outputs", []))
    if r.get("action") == "GUARDRAIL_INTERVENED":
        return (out or "We've temporarily held a transaction on your account for your protection. Please contact the bank."), "bedrock_guardrail_intervened"
    return text, None


def run_case(agent, event, backend, audit, cfg=DEFAULT_CFG, bedrock_runtime=None, guardrail_id=None, guardrail_version=None):
    rec = Recorder(); case_id = CASE_ID[agent](event); model_info = {}
    try:
        prop = backend.investigate(agent, event, rec); model_info = getattr(backend, "info", {})
    except Exception as e:     # an agent crash must never auto-resolve anything
        prop = Proposal("ESCALATE", 0.0, [f"agent failed: {type(e).__name__}: {e}"]); rec.trace.append({"type": "error", "note": str(e)})
    ev = verify.evidence_from_calls(rec.calls)
    decision = prop.decision if prop.decision in ALLOWED[agent] else "ESCALATE"
    conf = verify.cap_confidence(agent, prop.confidence, ev)
    why = list(prop.reasoning)
    if prop.decision not in ALLOWED[agent]: why.append(f"model returned invalid decision '{prop.decision}'")
    if conf < prop.confidence: why.append(f"confidence capped {prop.confidence:.2f} -> {conf:.2f} by evidence coverage")
    base_risk = None
    if agent == "fraud":
        base_risk, bwhy = verify.baseline_risk(event, ev); why.append(f"code-side baseline risk {base_risk}/100: " + "; ".join(bwhy))
    rec.trace.append({"step": rec.step + 1, "type": "decide", "proposed": decision, "risk_score": prop.risk_score, "confidence": conf, "reasoning": why})
    contra = verify.contradiction(agent, decision, event, ev, cfg)
    v = guardrails.apply(agent, {"decision": decision, "confidence": conf}, {"amount": stake(agent, event, ev), "contradiction": contra}, cfg)
    for t in v.triggered: rec.trace.append({"step": rec.step + 1, "type": "guardrail", **t})
    if not v.triggered: rec.trace.append({"step": rec.step + 1, "type": "guardrail", "rule": "none", "detail": "no guardrail fired; decision stands"})
    msg, msg_note = (None, None)
    if v.decision == "HOLD_AND_NOTIFY": msg, msg_note = sanitize_customer_message(bedrock_runtime, guardrail_id, guardrail_version, prop.customer_message)
    if msg_note: rec.trace.append({"step": rec.step + 1, "type": "guardrail", "rule": "BEDROCK_GUARDRAIL_OUTPUT", "detail": msg_note})
    undo = None if v.requires_human else undo_instruction(agent, v.decision, event)
    status = "PENDING_HUMAN" if v.requires_human else "AUTO_RESOLVED"
    amount = stake(agent, event, ev)
    summary = f"{case_id}: proposed {decision}" + (f" -> {v.decision} by guardrail" if v.decision != decision else "") + (f" | customer msg: {msg}" if msg else "")
    audit.write(case_id=case_id, agent=agent, event=event, tool_calls=rec.calls, reasoning_trace=rec.trace, proposed_decision=decision, final_decision=v.decision,
                risk_score=prop.risk_score if prop.risk_score is not None else base_risk, confidence=conf, guardrails=v.triggered, status=status,
                requires_human=int(v.requires_human), reversible=int(undo is not None), undo_instruction=undo, summary=summary, model_info=model_info)
    return {"case_id": case_id, "agent": agent, "decision": v.decision, "proposed": decision, "confidence": conf, "risk_score": prop.risk_score, "baseline_risk": base_risk,
            "guardrails": [t["rule"] for t in v.triggered], "requires_human": v.requires_human, "recommend_block": v.recommend_block, "status": status,
            "tools": [c["tool"] for c in rec.calls], "amount": amount}
