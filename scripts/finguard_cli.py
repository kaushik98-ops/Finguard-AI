"""Operator CLI against a deployed stack.
  python scripts/finguard_cli.py send --agent all [--limit 5]     # put events on the EventBridge bus -> Step Functions -> agents
  python scripts/finguard_cli.py dashboard [--status PENDING_HUMAN] [--agent fraud]
  python scripts/finguard_cli.py explain FRD-T01092
  python scripts/finguard_cli.py reverse FRD-T01049 --by me --reason "customer called"
"""
import argparse
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src")]
from finguard_core.audit import AuditLog  # noqa: E402


def outputs(stack):
    o = boto3.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return {x["OutputKey"].removeprefix("Out"): x["OutputValue"] for x in o}


def send(out, agent, limit):
    rd = lambda n: json.loads((ROOT / "data" / f"{n}.json").read_text()); ev = []
    if agent in ("kyc", "all"): ev += [{"type": "loan_document", "doc_id": d["doc_id"], "customer_id": d["customer_id"]} for d in rd("scenarios")["kyc_docs"][:limit]]
    if agent in ("fraud", "all"):
        ev += [{"type": "flagged_transaction", **{k: t[k] for k in ("txn_id", "customer_id", "ts", "amount", "merchant_id", "device_id", "geo", "rule_triggered")}} for t in rd("transactions") if t["flagged"]][:limit]
    if agent in ("recon", "all"): ev.append({"type": "reconciliation_batch"})
    eb = boto3.client("events")
    for i in range(0, len(ev), 10):
        eb.put_events(Entries=[{"EventBusName": out["EventBus"], "Source": "finguard.ingest", "DetailType": "FinGuardEvent", "Detail": json.dumps(e)} for e in ev[i:i + 10]])
    print(f"sent {len(ev)} events to bus {out['EventBus']}")


def dashboard(audit, agent, status):
    rows = audit.list_cases(agent=agent, status=status)
    hdr = ["CASE", "AGENT", "DECISION", "RISK", "CONF", "STATUS", "GUARDRAILS", "SUMMARY"]
    data = [[r["case_id"], r["agent"], r["final_decision"], r.get("risk_score", ""), f"{r.get('confidence', 0):.2f}", r["status"], ",".join(g["rule"].split("_")[0] for g in r.get("guardrails", [])), r["summary"][:60]] for r in rows]
    w = [max(len(str(x)) for x in col) for col in zip(hdr, *data)] if data else [len(h) for h in hdr]
    for row in [hdr] + data: print("  ".join(str(x).ljust(n) for x, n in zip(row, w)))
    print(f"\n{len(rows)} cases")


def explain(audit, case_id):
    for r in audit.get_case(case_id):
        print(f"=== {r['case_id']} [{r['record_type']}] {r['ts']} model={r.get('model_info', {}).get('model_id')}")
        if r["record_type"] != "decision": print(r["summary"]); continue
        for t in r["reasoning_trace"]:
            k = t["type"]
            if k == "plan": print(f"  PLAN    {t['thought']} -> {t['next']}")
            elif k == "observe": print(f"  OBSERVE {t['note']}")
            elif k == "decide": print(f"  DECIDE  {t['proposed']} conf={t['confidence']}" + "".join(f"\n            {x}" for x in t["reasoning"]))
            elif k == "guardrail": print(f"  GUARD   {t['rule']}: {t['detail']}")
        print(f"final: {r['final_decision']} (proposed {r['proposed_decision']}) status={r['status']}" + (f" | undo: {r['undo_instruction']}" if r.get("undo_instruction") else ""))


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--stack", default="FinGuardAws"); sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send"); s.add_argument("--agent", default="all", choices=["fraud", "kyc", "recon", "all"]); s.add_argument("--limit", type=int, default=None)
    d = sub.add_parser("dashboard"); d.add_argument("--agent"); d.add_argument("--status")
    e = sub.add_parser("explain"); e.add_argument("case_id")
    v = sub.add_parser("reverse"); v.add_argument("case_id"); v.add_argument("--by", required=True); v.add_argument("--reason", required=True)
    a = p.parse_args(); out = outputs(a.stack); audit = AuditLog(boto3.resource("dynamodb").Table(out["AuditTable"]))
    if a.cmd == "send": send(out, a.agent, a.limit)
    elif a.cmd == "dashboard": dashboard(audit, a.agent, a.status)
    elif a.cmd == "explain": explain(audit, a.case_id)
    else: audit.reverse(a.case_id, a.by, a.reason); print("reversed; case returned to the human queue")
