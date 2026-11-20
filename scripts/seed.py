"""Generate synthetic data and load it into DynamoDB + S3.  python scripts/seed.py --data-table T --bucket B"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
from finguard_core.repo import Repo  # noqa: E402


def load(repo: Repo, s3, bucket, data_dir: Path):
    rd = lambda n: json.loads((data_dir / f"{n}.json").read_text())
    items, pans = [], {}
    for c in rd("customers"):
        items.append((f"CUSTOMER#{c['customer_id']}", "PROFILE", {"data": c})); pans.setdefault(c["pan"], []).append(c["customer_id"])
    items += [(f"PAN#{p}", "MAP", {"customer_ids": ids}) for p, ids in pans.items()]
    items += [(f"MERCHANT#{m['merchant_id']}", "PROFILE", {"data": m}) for m in rd("merchants")]
    items += [(f"CUSTOMER#{t['customer_id']}", f"TXN#{t['ts']}#{t['txn_id']}", {"data": t}) for t in rd("transactions")]
    items += [(f"CUSTOMER#{d['customer_id']}", f"DISPUTE#{d['dispute_id']}", {"data": d}) for d in rd("disputes")]
    items += [(f"BLACKLIST#{p}", "ENTRY", {}) for p in rd("policy")["blacklisted_pans"]]
    repo.put_many(items)
    for pdf in (data_dir / "docs").glob("*.pdf"): s3.upload_file(str(pdf), bucket, f"docs/{pdf.name}")
    s3.upload_file(str(data_dir / "ledger_core.csv"), bucket, "ledgers/core/ledger_core.csv")
    s3.upload_file(str(data_dir / "ledger_settlement.csv"), bucket, "ledgers/settlement/ledger_settlement.csv")
    return len(items)


if __name__ == "__main__":
    import boto3, data_gen
    ap = argparse.ArgumentParser(); ap.add_argument("--data-table", required=True); ap.add_argument("--bucket", required=True); a = ap.parse_args()
    data_gen.generate()
    n = load(Repo(boto3.resource("dynamodb").Table(a.data_table)), boto3.client("s3"), a.bucket, ROOT / "data")
    print(f"loaded {n} items into {a.data_table}; uploaded documents + ledgers to s3://{a.bucket}")
