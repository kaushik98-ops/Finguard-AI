"""Configuration. Guardrail thresholds are enforced in CODE (guardrails.py); env vars (FG_<NAME>) can override them per deployment."""
import os
from dataclasses import dataclass, fields
from datetime import datetime

REF_NOW = datetime(2026, 10, 12, 10, 0, 0)   # fixed 'now' for the synthetic dataset / recon batch date


@dataclass(frozen=True)
class GuardrailConfig:
    min_confidence: float = 0.70
    fraud_auto_clear_max_risk: int = 30
    fraud_hold_max_risk: int = 65
    fraud_block_min_risk: int = 85
    fraud_auto_clear_max_value: float = 100_000
    fraud_auto_block_max_value: float = 50_000
    kyc_auto_approve_max_loan: float = 5_000_000
    kyc_max_loan_to_annual_income: float = 5.0
    kyc_min_monthly_income: float = 25_000
    recon_rounding_tolerance: float = 1.00
    recon_auto_resolve_max_value: float = 100_000
    recon_defer_ack_value: float = 2_500_000

    @classmethod
    def from_env(cls):
        kw = {}
        for f in fields(cls):
            v = os.getenv("FG_" + f.name.upper())
            if v is not None: kw[f.name] = type(f.default)(v)
        return cls(**kw)


DEFAULT_CFG = GuardrailConfig()


def env(name, default=None):
    return os.environ.get(name, default)
