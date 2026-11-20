"""DynamoDB access layer (single 'data' table: pk/sk entity design)."""
import json
from decimal import Decimal

from boto3.dynamodb.conditions import Key


def to_ddb(o): return json.loads(json.dumps(o, default=str), parse_float=Decimal)


def from_ddb(o):
    if isinstance(o, Decimal): return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, dict): return {k: from_ddb(v) for k, v in o.items()}
    if isinstance(o, list): return [from_ddb(v) for v in o]
    return o


def query_all(table, **kw):
    """Paginated query/scan helper."""
    items, fn = [], table.query if "KeyConditionExpression" in kw else table.scan
    while True:
        r = fn(**kw); items += r["Items"]
        if "LastEvaluatedKey" not in r: return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


class Repo:
    def __init__(self, table): self.t = table

    def _q(self, pk, sk_cond=None):
        cond = Key("pk").eq(pk) & sk_cond if sk_cond is not None else Key("pk").eq(pk)
        return [from_ddb(i) for i in query_all(self.t, KeyConditionExpression=cond)]

    def _get(self, pk, sk):
        i = self.t.get_item(Key={"pk": pk, "sk": sk}).get("Item"); return from_ddb(i) if i else None

    def put(self, pk, sk, **attrs): self.t.put_item(Item=to_ddb({"pk": pk, "sk": sk, **attrs}))

    def put_many(self, items):
        with self.t.batch_writer() as w:
            for pk, sk, attrs in items: w.put_item(Item=to_ddb({"pk": pk, "sk": sk, **attrs}))

    # ---- core-banking style entities
    def customer(self, cid): i = self._get(f"CUSTOMER#{cid}", "PROFILE"); return i["data"] if i else None
    def txns(self, cid, lo, hi):   # lo <= ts < hi  (ISO timestamps sort lexically)
        return [i["data"] for i in self._q(f"CUSTOMER#{cid}", Key("sk").between(f"TXN#{lo}", f"TXN#{hi}")) if i["sk"] < f"TXN#{hi}"]
    def disputes(self, cid): return [i["data"] for i in self._q(f"CUSTOMER#{cid}", Key("sk").begins_with("DISPUTE#"))]
    def merchant(self, mid): i = self._get(f"MERCHANT#{mid}", "PROFILE"); return i["data"] if i else None
    def customers_by_pan(self, pan): i = self._get(f"PAN#{pan}", "MAP"); return i["customer_ids"] if i else []
    def is_blacklisted(self, pan): return self._get(f"BLACKLIST#{pan}", "ENTRY") is not None

    # ---- working state shared between stateless tool invocations
    def save_doc_fields(self, doc_id, fields): self.put(f"DOC#{doc_id}", "FIELDS", data=fields)
    def doc_fields(self, doc_id): i = self._get(f"DOC#{doc_id}", "FIELDS"); return i["data"] if i else None
    def save_mismatches(self, batch, mms): self.put_many([(f"RECON#{batch}", f"MM#{m['mismatch_id']}", {"data": m}) for m in mms])
    def mismatch(self, batch, mid): i = self._get(f"RECON#{batch}", f"MM#{mid}"); return i["data"] if i else None
    def mismatches(self, batch): return [i["data"] for i in self._q(f"RECON#{batch}", Key("sk").begins_with("MM#"))]
