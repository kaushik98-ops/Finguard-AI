"""Reconciliation batch steps around the per-mismatch agent (run by Step Functions): prepare (load + diff) and summarize."""
from finguard_core.config import REF_NOW
from handlers import common


def prepare(event, context):
    batch_id = event.get("batch_id") or f"RECBATCH-{event.get('batch_date', REF_NOW.date())}"
    r = common.tools().diff_ledgers(batch_id, event.get("core_key", "ledgers/core/ledger_core.csv"), event.get("settlement_key", "ledgers/settlement/ledger_settlement.csv"))
    common.audit().write(case_id=batch_id, agent="recon", record_type="batch", event=event, status="INFO",
                         tool_calls=[{"step": 1, "tool": "diff_ledgers", "args": {"batch_id": batch_id}, "result": {k: v for k, v in r.items() if k != "mismatch_events"}}],
                         summary=f"Loaded {r['core_rows']} core / {r['settlement_rows']} settlement rows; {r['mismatches']} mismatches")
    return {"batch_id": batch_id, "mismatches": r["mismatch_events"]}


def summarize(event, context):
    results, batch_id = event["results"], event["batch_id"]
    by = lambda d: [r for r in results if r["decision"] == d]
    s = {"batch_id": batch_id, "mismatches": len(results), "auto_resolved": len(by("AUTO_RESOLVE")), "deferred": len(by("DEFER")), "flagged_for_reversal": len(by("FLAG_REVERSAL")),
         "escalated": len(by("ESCALATE")), "value_escalated": round(sum(r["amount"] for r in by("ESCALATE")), 2), "value_pending_reversal": round(sum(r["amount"] for r in by("FLAG_REVERSAL")), 2)}
    s["total_value_at_risk"] = round(s["value_escalated"] + s["value_pending_reversal"], 2)
    common.audit().write(case_id=f"{batch_id}-SUMMARY", agent="recon", record_type="batch", event=s, status="INFO", summary=f"Daily reconciliation summary: {s}")
    return s
