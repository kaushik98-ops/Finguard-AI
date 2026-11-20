"""Shared Audit & Case-Log store on DynamoDB.
Table: pk=case_id, sk='<iso-ts>#<rand>'. GSIs: by_agent(agent, ts), by_status(status, ts) -> 'what is waiting for a human?'.
Everything needed to explain or reverse a decision is on the record; history is append-only (reversal = new record)."""
import json
import uuid
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .repo import query_all

MAX_RESULT_BYTES = 12_000   # per tool result, to stay far under DynamoDB's 400 KB item limit
JSON_FIELDS = ("event", "tool_calls", "reasoning_trace", "guardrails", "model_info")


def now(): return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _trim(calls):
    out = []
    for c in calls:
        s = json.dumps(c.get("result"), default=str)
        out.append({**c, "result": c["result"] if len(s) <= MAX_RESULT_BYTES else {"_truncated": True, "preview": s[:MAX_RESULT_BYTES]}})
    return out


class AuditLog:
    def __init__(self, table): self.t = table

    def write(self, **rec):
        ts = now()
        item = {"case_id": rec["case_id"], "sk": f"{ts}#{uuid.uuid4().hex[:6]}", "ts": ts, "agent": rec["agent"], "record_type": rec.get("record_type", "decision"),
                "status": rec.get("status", "INFO"), "requires_human": int(rec.get("requires_human", 0)), "reversible": int(rec.get("reversible", 0)),
                "proposed_decision": rec.get("proposed_decision") or "-", "final_decision": rec.get("final_decision") or "-", "summary": rec.get("summary", "")}
        for k in ("risk_score", "confidence"):
            if rec.get(k) is not None: item[k] = str(rec[k])
        if rec.get("undo_instruction"): item["undo_instruction"] = rec["undo_instruction"]
        for k in JSON_FIELDS:
            if rec.get(k) is not None: item[k] = json.dumps(_trim(rec[k]) if k == "tool_calls" else rec[k], default=str)
        self.t.put_item(Item=item)
        return item["sk"]

    @staticmethod
    def _hydrate(i):
        d = dict(i)
        for k in JSON_FIELDS:
            if k in d: d[k] = json.loads(d[k])
        for k in ("risk_score", "confidence"):
            if k in d: d[k] = float(d[k])
        d["requires_human"], d["reversible"] = int(d["requires_human"]), int(d["reversible"])
        return d

    def get_case(self, case_id):
        return [self._hydrate(i) for i in query_all(self.t, KeyConditionExpression=Key("case_id").eq(case_id))]

    def latest(self, case_id):
        recs = [r for r in self.get_case(case_id) if r["record_type"] == "decision"]; return recs[-1] if recs else None

    def list_cases(self, agent=None, status=None):
        if status: items = query_all(self.t, IndexName="by_status", KeyConditionExpression=Key("status").eq(status))
        elif agent: items = query_all(self.t, IndexName="by_agent", KeyConditionExpression=Key("agent").eq(agent))
        else: items = query_all(self.t)
        rows = [self._hydrate(i) for i in items if i["record_type"] == "decision"]
        if agent: rows = [r for r in rows if r["agent"] == agent]
        return sorted(rows, key=lambda r: r["ts"])

    def reverse(self, case_id, by, reason):
        rec = self.latest(case_id)
        if rec is None: raise KeyError(f"unknown case {case_id}")
        if not rec["reversible"]: raise ValueError(f"{case_id}: decision {rec['final_decision']} is not reversible (already human-owned)")
        try:   # atomic guard against double reversal
            self.t.update_item(Key={"case_id": case_id, "sk": rec["sk"]}, UpdateExpression="SET #s = :r",
                               ConditionExpression=Attr("status").ne("REVERSED") & Attr("reversible").eq(1),
                               ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":r": "REVERSED"})
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException": raise ValueError(f"{case_id}: already reversed") from e
            raise
        return self.write(case_id=case_id, agent=rec["agent"], record_type="reversal", status="REVERSED", requires_human=1,
                          event={"reversed_sk": rec["sk"], "by": by, "reason": reason}, proposed_decision=rec["final_decision"], final_decision="REVERSED_TO_HUMAN_QUEUE",
                          summary=f"Auto-resolution '{rec['final_decision']}' reversed by {by}: {reason}. Undo: {rec.get('undo_instruction')}")
