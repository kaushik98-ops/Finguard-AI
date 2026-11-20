"""Stand-in 'models' for tests. They call the REAL tool implementations and return Proposals, exactly like the Strands backend would.
ScriptedModel = a sensible investigator. OverconfidentModel = a hallucinating/unsafe one, to prove the code-side checks catch it."""
from finguard_core import fraud_logic as fl
from finguard_core import verify
from finguard_core.config import DEFAULT_CFG
from finguard_core.runner import Proposal
from finguard_core.tools import PAN_RE, dispatch


class ScriptedModel:
    info = {"model_id": "scripted-test-model"}

    def __init__(self, tools): self.tools = tools

    def _call(self, rec, tool, args, why):
        rec.plan(why, [tool]); r = dispatch(self.tools, tool, args); rec.call(tool, args, r); return r

    def investigate(self, agent, event, rec):
        return getattr(self, agent)(event, rec)

    def fraud(self, e, rec):
        c = e["customer_id"]; geo = e["geo"]
        self._call(rec, "get_transaction_history", {"customer_id": c, "before_ts": e["ts"]}, "baseline first")
        self._call(rec, "check_device_geo_consistency", {"customer_id": c, "device_id": e["device_id"], "lat": geo["lat"], "lon": geo["lon"], "country": geo["country"], "ts": e["ts"]}, "device/geo")
        self._call(rec, "check_merchant_risk", {"merchant_id": e["merchant_id"]}, "merchant")
        ev = verify.evidence_from_calls(rec.calls); risk, why = fl.score(e, ev)
        if 0 < risk < 85 and risk > 10:
            self._call(rec, "get_dispute_history", {"customer_id": c}, "ambiguous: check disputes"); ev = verify.evidence_from_calls(rec.calls); risk, why = fl.score(e, ev)
        return Proposal(fl.decision_for(risk, DEFAULT_CFG), round(0.4 + 0.6 * fl.coverage(ev) + 0.05, 2), why, risk, "Your transaction is on hold; call us. Contact 9876543210." if risk > 30 else None)

    def kyc(self, e, rec):
        d, cid = e["doc_id"], e["customer_id"]
        r = self._call(rec, "extract_document_fields", {"doc_id": d}, "extract")
        if r["warnings"] and not PAN_RE.match(r["fields"].get("pan", "")):
            r = self._call(rec, "extract_document_fields", {"doc_id": d, "strategy": "ocr_strict"}, "PAN malformed - maybe OCR misread; retry strict")
        self._call(rec, "lookup_customer_record", {"customer_id": cid}, "record")
        cc = self._call(rec, "cross_check_fields", {"doc_id": d, "customer_id": cid}, "cross-check")
        pol = self._call(rec, "check_policy_rules", {"doc_id": d}, "policy")
        owner = None
        if any(m.startswith(("PAN", "name similarity")) for m in cc["mismatches"]) and r["fields"].get("pan"):
            owner = self._call(rec, "find_customer_by_pan", {"pan": r["fields"]["pan"]}, "who owns this PAN?")
        bl = any("BLACKLIST" in x for x in pol["failed"]); other = bool(owner and owner["customer_ids"] and cid not in owner["customer_ids"])
        if bl or other: return Proposal("ESCALATE", .9, ["identity/forgery indicators"])
        if pol["failed"] or [m for m in cc["mismatches"] if not m.startswith("name similarity")]: return Proposal("FLAG_MISSING_INFO", .9, pol["failed"] + cc["mismatches"])
        return Proposal("AUTO_APPROVE", .95, ["all checks passed"])

    def recon(self, e, rec):
        a = {"batch_id": e["batch_id"], "mismatch_id": e["mismatch_id"]}
        c = self._call(rec, "classify_mismatch", a, "classify")
        cls, conf = c["classification"], c["confidence"]
        if cls == "BREAK" or conf < .8:
            s = self._call(rec, "search_related_entries", a, "look for counterpart before escalating")
            if any(x["normalised_equal"] for x in s["counterparts"]): cls, conf = "REF_FORMAT", .9
        d = {"TIMING": "DEFER", "ROUNDING": "AUTO_RESOLVE", "REF_FORMAT": "AUTO_RESOLVE", "DUPLICATE": "FLAG_REVERSAL", "BREAK": "ESCALATE"}[cls]
        return Proposal(d, conf, [f"classified {cls}"])


class OverconfidentModel:
    """Skips investigation and confidently proposes the most permissive automatic action."""
    info = {"model_id": "overconfident-test-model"}
    def investigate(self, agent, event, rec):
        rec.plan("Looks fine to me, no need to check anything.")
        return Proposal({"fraud": "AUTO_CLEAR", "kyc": "AUTO_APPROVE", "recon": "AUTO_RESOLVE"}[agent], 0.99, ["trust me"], 5)
