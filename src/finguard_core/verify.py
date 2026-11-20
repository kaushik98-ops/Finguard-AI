"""Code-side verification of the model's proposal against the evidence its own tool calls produced.
The LLM chooses the investigation path and writes the reasoning; this module independently checks that the
proposed AUTOMATIC action is actually supported by the evidence. Disagreement -> guardrail G9 -> human."""
from . import fraud_logic as fl

KYC_W = {"extract_document_fields": .3, "lookup_customer_record": .25, "cross_check_fields": .25, "check_policy_rules": .2}


def evidence_from_calls(calls):
    ev = {}
    for c in calls:
        if "error" not in c["result"]: ev[c["tool"]] = c["result"]      # latest successful result per tool
    return ev


def coverage(agent, ev):
    if agent == "fraud": return fl.coverage(ev)
    if agent == "kyc": return sum(w for t, w in KYC_W.items() if t in ev)
    return 1.0 if "classify_mismatch" in ev else 0.0


def cap_confidence(agent, conf, ev):
    """Model-reported confidence can never exceed what the evidence coverage justifies."""
    cov = coverage(agent, ev)
    return round(min(max(conf, 0.0), 0.4 + 0.6 * cov, 0.97), 2)


def baseline_risk(event, ev):
    return fl.score(event, ev)


def contradiction(agent, decision, event, ev, cfg):
    """Return a reason string if an AUTOMATIC decision is not supported by the evidence, else None."""
    if agent == "fraud":
        if decision not in ("AUTO_CLEAR", "HOLD_AND_NOTIFY", "AUTO_BLOCK"): return None
        risk, _ = fl.score(event, ev); want = fl.decision_for(risk, cfg)
        if decision != want: return f"model proposed {decision} but code-side evidence score is {risk}/100 which supports {want}"
        return None
    if agent == "kyc":
        if decision != "AUTO_APPROVE": return None
        missing = [t for t in KYC_W if t not in ev]
        if missing: return f"AUTO_APPROVE without completing checks: {', '.join(missing)}"
        if ev["check_policy_rules"]["failed"]: return f"AUTO_APPROVE but policy rules failed: {ev['check_policy_rules']['failed']}"
        name_sim = ev["cross_check_fields"].get("checks", {}).get("name", 1)
        hard = [m for m in ev["cross_check_fields"]["mismatches"] if not (m.startswith("name similarity") and name_sim >= 0.9)]
        if hard: return f"AUTO_APPROVE but cross-check mismatches remain: {hard}"
        return None
    if agent == "recon":
        if decision not in ("AUTO_RESOLVE", "DEFER", "FLAG_REVERSAL"): return None
        if "classify_mismatch" not in ev: return f"{decision} without classifying the mismatch"
        cls = ev["classify_mismatch"]["classification"]; s = ev.get("search_related_entries")
        ref_fmt = bool(s and any(x["normalised_equal"] for x in s["counterparts"]))
        ok = {"AUTO_RESOLVE": cls == "ROUNDING" or ref_fmt, "DEFER": cls == "TIMING", "FLAG_REVERSAL": cls == "DUPLICATE"}[decision]
        return None if ok else f"{decision} not supported: classification={cls}, ref-format counterpart={ref_fmt}"
    return None
