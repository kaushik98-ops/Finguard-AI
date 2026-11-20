"""Shared guardrail layer — deterministic, enforced in code AFTER the model proposes a decision.
(Bedrock Guardrails handle content/PII/prompt-attack on model I/O; they cannot enforce business limits.)"""
from dataclasses import dataclass, field

from .config import DEFAULT_CFG, GuardrailConfig

AUTO_DECISIONS = {"fraud": {"AUTO_CLEAR", "HOLD_AND_NOTIFY", "AUTO_BLOCK"}, "kyc": {"AUTO_APPROVE"},
                  "recon": {"AUTO_RESOLVE", "DEFER", "FLAG_REVERSAL"}}
ALLOWED = {"fraud": AUTO_DECISIONS["fraud"] | {"ESCALATE"}, "kyc": {"AUTO_APPROVE", "FLAG_MISSING_INFO", "ESCALATE"},
           "recon": AUTO_DECISIONS["recon"] | {"ESCALATE"}}
HUMAN_DECISIONS = {"ESCALATE", "FLAG_MISSING_INFO"}


@dataclass
class Verdict:
    decision: str
    triggered: list = field(default_factory=list)
    requires_human: bool = False
    recommend_block: bool = False


def _fire(v, rule, detail, to):
    v.triggered.append({"rule": rule, "detail": detail, "from": v.decision, "to": to}); v.decision = to; v.requires_human = True


def apply(agent, proposed, ctx, cfg: GuardrailConfig = DEFAULT_CFG) -> Verdict:
    """proposed: {decision, confidence}. ctx: {amount, contradiction(optional str)}."""
    v = Verdict(decision=proposed["decision"]); amount = float(ctx.get("amount") or 0); conf = proposed.get("confidence", 0.0)
    if v.decision not in ALLOWED[agent]:
        _fire(v, "G0_INVALID_DECISION", f"'{v.decision}' is not a permitted decision for the {agent} agent", "ESCALATE")
    if v.decision in AUTO_DECISIONS[agent] and ctx.get("contradiction"):
        _fire(v, "G9_EVIDENCE_CONTRADICTION", ctx["contradiction"], "ESCALATE")
    if v.decision in AUTO_DECISIONS[agent] and conf < cfg.min_confidence:
        _fire(v, "G1_LOW_CONFIDENCE", f"confidence {conf:.2f} < minimum {cfg.min_confidence:.2f}; auto-resolution forbidden", "ESCALATE")
    if agent == "fraud":
        if v.decision == "AUTO_BLOCK" and amount > cfg.fraud_auto_block_max_value:
            _fire(v, "G2_BLOCK_VALUE_LIMIT", f"amount {amount:,.0f} > auto-block limit {cfg.fraud_auto_block_max_value:,.0f}; agent may only RECOMMEND a block, analyst must confirm", "ESCALATE")
            v.recommend_block = True
        if v.decision == "AUTO_CLEAR" and amount > cfg.fraud_auto_clear_max_value:
            _fire(v, "G3_CLEAR_VALUE_LIMIT", f"amount {amount:,.0f} > auto-clear limit {cfg.fraud_auto_clear_max_value:,.0f}", "ESCALATE")
    elif agent == "kyc":
        if v.decision == "AUTO_APPROVE" and amount > cfg.kyc_auto_approve_max_loan:
            _fire(v, "G5_LOAN_VALUE_LIMIT", f"loan {amount:,.0f} > auto-forward limit {cfg.kyc_auto_approve_max_loan:,.0f}; senior review", "ESCALATE")
    elif agent == "recon":
        if v.decision in ("AUTO_RESOLVE", "FLAG_REVERSAL") and amount > cfg.recon_auto_resolve_max_value:
            _fire(v, "G6_RESOLVE_VALUE_LIMIT", f"value {amount:,.0f} > auto-resolve limit {cfg.recon_auto_resolve_max_value:,.0f}", "ESCALATE")
        if v.decision == "DEFER" and amount > cfg.recon_defer_ack_value:
            _fire(v, "G7_DEFER_ACK", f"timing difference {amount:,.0f} > {cfg.recon_defer_ack_value:,.0f}; analyst must acknowledge", "ESCALATE")
    if v.decision in HUMAN_DECISIONS: v.requires_human = True
    return v
