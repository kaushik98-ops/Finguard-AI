"""Tool implementations + their schemas (single source of truth).
 - exposed to agents through AgentCore Gateway (Lambda targets) using TOOL_SPECS
 - stateless: anything that must survive between calls (extracted doc fields, mismatches) lives in DynamoDB
Tools take small primitive arguments / ids (not big JSON blobs) so the model can't corrupt evidence by re-typing it."""
import csv
import io
import re
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from .config import DEFAULT_CFG, REF_NOW, env
from .fraud_logic import FMT, haversine, parse

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
KEYMAP = {"Full Name": "full_name", "Date of Birth": "dob", "PAN": "pan", "Address": "address", "Employer": "employer", "Monthly Income (INR)": "monthly_income",
          "Loan Amount Requested (INR)": "loan_amount", "Photo ID Expiry": "id_expiry", "Customer ID": "customer_id", "Application ID": "application_id", "Loan Purpose": "loan_purpose"}
REQUIRED = ["full_name", "dob", "pan", "address", "monthly_income", "loan_amount", "id_expiry"]
TO_DIGIT, TO_LETTER = {"O": "0", "I": "1", "S": "5", "B": "8", "Z": "2"}, {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z"}
norm_ref = lambda r: r.strip().upper()


class Tools:
    """Dependencies injected so tests can use moto / fakes."""
    def __init__(self, repo, s3=None, textract=None, bedrock_agent_rt=None, bucket=None, kb_id=None, cfg=DEFAULT_CFG):
        self.repo, self.s3, self.textract, self.bar, self.bucket, self.kb_id, self.cfg = repo, s3, textract, bedrock_agent_rt, bucket, kb_id, cfg

    # ============================== FRAUD ==============================
    def get_transaction_history(self, customer_id, before_ts, days=90):
        lo = (parse(before_ts) - timedelta(days=int(days))).strftime(FMT)
        rows = self.repo.txns(customer_id, lo, before_ts[:19]); amts = [t["amount"] for t in rows]
        mean = sum(amts) / len(amts) if amts else 0.0
        std = (sum((a - mean) ** 2 for a in amts) / len(amts)) ** .5 if len(amts) > 1 else 0.0
        return {"count": len(rows), "mean": round(mean, 2), "std": round(std, 2), "max": max(amts, default=0.0), "merchant_ids": sorted({t["merchant_id"] for t in rows}),
                "countries": sorted({t["geo"]["country"] for t in rows}), "recent": [{"txn_id": t["txn_id"], "ts": t["ts"], "amount": t["amount"], "merchant_id": t["merchant_id"]} for t in rows[-5:]]}

    def check_merchant_risk(self, merchant_id):
        m = self.repo.merchant(merchant_id)
        if not m: return {"error": f"merchant {merchant_id} not found in reputation database"}
        return {k: m[k] for k in ("merchant_id", "risk_score", "chargeback_rate", "age_days", "category")}

    def check_device_geo_consistency(self, customer_id, device_id, lat, lon, country, ts):
        c = self.repo.customer(customer_id)
        if not c: return {"error": f"customer {customer_id} not found"}
        hg = c["home_geo"]; prior = self.repo.txns(customer_id, "0000", ts[:19]); last = prior[-1] if prior else None
        out = {"known_device": device_id in c["known_devices"], "distance_from_home_km": round(haversine(lat, lon, hg["lat"], hg["lon"])), "foreign": country != "IN", "country": country, "impossible_travel": False}
        if last:
            hrs = max((parse(ts) - parse(last["ts"])).total_seconds() / 3600, 0.01); km = haversine(lat, lon, last["geo"]["lat"], last["geo"]["lon"])
            out.update(last_txn_hours_ago=round(hrs, 1), km_from_last_txn=round(km), implied_speed_kmh=round(km / hrs)); out["impossible_travel"] = km > 300 and km / hrs > 900
        return out

    def get_dispute_history(self, customer_id):
        d = self.repo.disputes(customer_id)
        return {"count": len(d), "fraud_confirmed": sum(bool(x["fraud_confirmed"]) for x in d), "reasons": [x["reason"] for x in d]}

    def get_related_transactions(self, customer_id, ts, window_hours=24):
        lo = (parse(ts) - timedelta(hours=int(window_hours))).strftime(FMT)
        rows = [t for t in self.repo.txns(customer_id, lo, ts[:19])]
        by = defaultdict(int)
        for t in rows: by[t["merchant_id"]] += 1
        return {"window_hours": int(window_hours), "count": len(rows), "max_same_merchant": max(by.values(), default=0), "total_amount": round(sum(t["amount"] for t in rows), 2)}

    # ============================== KYC ==============================
    def extract_document_fields(self, doc_id, strategy="default"):
        kv = self._textract_kv(f"docs/{doc_id}.pdf"); fields = {}
        for k, v in kv.items():
            if k in KEYMAP: fields[KEYMAP[k]] = v
        for k in ("monthly_income", "loan_amount"):
            if k in fields: fields[k] = float(re.sub(r"[^\d.]", "", str(fields[k])) or 0)
        warnings = []
        if "pan" in fields:
            if strategy == "ocr_strict":
                fixed = "".join((TO_DIGIT.get(ch, ch) if 5 <= i <= 8 else TO_LETTER.get(ch, ch)) for i, ch in enumerate(fields["pan"]))
                if fixed != fields["pan"]: warnings.append(f"ocr_strict corrected PAN {fields['pan']} -> {fixed}")
                fields["pan"] = fixed
            if not PAN_RE.match(fields["pan"]): warnings.append(f"PAN '{fields['pan']}' fails format check")
        self.repo.save_doc_fields(doc_id, fields)       # downstream tools read fields by doc_id
        return {"doc_id": doc_id, "strategy": strategy, "fields": fields, "warnings": warnings}

    def _textract_kv(self, key):
        """Amazon Textract AnalyzeDocument (FORMS). Also parses 'Key: Value' LINE blocks (simple forms are often returned as lines)."""
        blocks = self.textract.analyze_document(Document={"S3Object": {"Bucket": self.bucket, "Name": key}}, FeatureTypes=["FORMS"])["Blocks"]
        byid = {b["Id"]: b for b in blocks}
        def text(b): return " ".join(byid[c]["Text"] for r in b.get("Relationships", []) if r["Type"] == "CHILD" for c in r["Ids"] if byid[c]["BlockType"] == "WORD")
        kv = {}
        for b in blocks:
            if b["BlockType"] == "KEY_VALUE_SET" and "KEY" in b.get("EntityTypes", []):
                val = next((byid[i] for r in b.get("Relationships", []) if r["Type"] == "VALUE" for i in r["Ids"]), None)
                if val: kv[text(b).rstrip(":").strip()] = text(val)
        for b in blocks:
            if b["BlockType"] == "LINE" and ":" in b["Text"]:
                k, v = b["Text"].split(":", 1); kv.setdefault(k.strip(), v.strip())
        return kv

    def lookup_customer_record(self, customer_id):
        c = self.repo.customer(customer_id); return {"found": bool(c), "record": c}

    def find_customer_by_pan(self, pan):
        return {"pan": pan, "customer_ids": self.repo.customers_by_pan(pan), "blacklisted": self.repo.is_blacklisted(pan)}

    def cross_check_fields(self, doc_id, customer_id):
        f, rec = self.repo.doc_fields(doc_id), self.repo.customer(customer_id)
        if not f: return {"error": f"no extracted fields for {doc_id}; call extract_document_fields first"}
        if not rec: return {"checks": {}, "mismatches": [f"customer {customer_id} not found"]}
        sim = lambda a, b: round(SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio(), 2)
        chk = {"name": sim(f.get("full_name", ""), rec["name"]), "dob": f.get("dob") == rec["dob"], "pan": f.get("pan") == rec["pan"], "address": sim(f.get("address", ""), rec["address"])}
        mm = []
        if chk["name"] < 0.97: mm.append(f"name similarity {chk['name']} ('{f.get('full_name')}' vs '{rec['name']}')")
        if not chk["dob"]: mm.append("date of birth differs from record")
        if not chk["pan"]: mm.append("PAN differs from record")
        if chk["address"] < 0.85: mm.append(f"address differs from record (similarity {chk['address']})")
        inc = f.get("monthly_income")
        if inc and inc > 1.5 * rec["monthly_income"]: mm.append(f"stated income {inc:,.0f} is {inc / rec['monthly_income']:.1f}x the income on record ({rec['monthly_income']:,.0f})")
        return {"checks": chk, "mismatches": mm}

    def check_policy_rules(self, doc_id):
        f = self.repo.doc_fields(doc_id)
        if not f: return {"error": f"no extracted fields for {doc_id}; call extract_document_fields first"}
        cfg, res = self.cfg, []
        rule = lambda rid, ok, d: res.append({"rule": rid, "passed": bool(ok), "detail": d})
        missing = [x for x in REQUIRED if x not in f]; rule("REQUIRED_FIELDS", not missing, "all required fields present" if not missing else f"missing: {', '.join(missing)}")
        pan = f.get("pan", ""); rule("KYC-1 PAN_FORMAT", PAN_RE.match(pan), f"PAN '{pan}'")
        bl = self.repo.is_blacklisted(pan); rule("KYC-2 BLACKLIST", not bl, "PAN is on the fraud blacklist" if bl else "not blacklisted")
        if "id_expiry" in f:
            exp = datetime.strptime(f["id_expiry"], "%Y-%m-%d").date(); rule("KYC-3 ID_VALID", exp >= REF_NOW.date(), f"photo ID expires {exp}")
        inc, loan = f.get("monthly_income"), f.get("loan_amount")
        if inc is not None:
            rule("LND-1 MIN_INCOME", inc >= cfg.kyc_min_monthly_income, f"monthly income {inc:,.0f}")
            if loan is not None:
                cap = cfg.kyc_max_loan_to_annual_income * inc * 12; rule("LND-2 LOAN_TO_INCOME", loan <= cap, f"loan {loan:,.0f} vs cap {cap:,.0f}")
        failed = [r["rule"] for r in res if not r["passed"]]
        return {"results": res, "failed": failed, "policy_citations": self._kb_retrieve(" ".join(r["detail"] for r in res if not r["passed"]) or "KYC loan policy")}

    def _kb_retrieve(self, query, k=3):
        """RAG over the policy documents via Bedrock Knowledge Bases (OpenSearch Serverless)."""
        if not (self.bar and self.kb_id): return []
        r = self.bar.retrieve(knowledgeBaseId=self.kb_id, retrievalQuery={"text": query[:900]}, retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": k}})
        return [x["content"]["text"][:400] for x in r.get("retrievalResults", [])]

    # ============================== RECONCILIATION ==============================
    def load_ledger(self, key):
        rows = list(csv.DictReader(io.StringIO(self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode())))
        for r in rows: r["amount"] = float(r["amount"])
        return rows

    def diff_ledgers(self, batch_id, core_key, settlement_key):
        A, B = self.load_ledger(core_key), self.load_ledger(settlement_key); ga, gb = defaultdict(list), defaultdict(list)
        for r in A: ga[r["ref"]].append(r)
        for r in B: gb[r["ref"]].append(r)
        mm = []
        def add(kind, ref, a, b, amt): mm.append({"mismatch_id": f"MM-{len(mm) + 1:03d}", "kind": kind, "ref": ref, "core": a, "settlement": b, "amount": amt})
        for ref in sorted(set(ga) | set(gb)):
            a, b = ga.get(ref, []), gb.get(ref, [])
            if len(a) > 1 or len(b) > 1: add("duplicate_ref", ref, a, b, max(r["amount"] for r in a + b))
            elif a and not b: add("missing_in_settlement", ref, a, b, a[0]["amount"])
            elif b and not a: add("missing_in_core", ref, a, b, b[0]["amount"])
            elif abs(a[0]["amount"] - b[0]["amount"]) > 1e-9: add("amount_mismatch", ref, a, b, max(a[0]["amount"], b[0]["amount"]))
        self.repo.save_mismatches(batch_id, mm)
        return {"batch_id": batch_id, "core_rows": len(A), "settlement_rows": len(B), "mismatches": len(mm), "mismatch_events": [
            {"type": "mismatch", "batch_id": batch_id, "mismatch_id": m["mismatch_id"], "kind": m["kind"], "ref": m["ref"], "amount": m["amount"]} for m in mm]}

    def classify_mismatch(self, batch_id, mismatch_id):
        m = self.repo.mismatch(batch_id, mismatch_id)
        if not m: return {"error": f"unknown mismatch {mismatch_id}"}
        k, today, cfg = m["kind"], REF_NOW.date(), self.cfg
        if k == "duplicate_ref":
            rows = m["core"] if len(m["core"]) > 1 else m["settlement"]
            if len({(r["amount"], r["date"]) for r in rows}) == 1: return {"classification": "DUPLICATE", "confidence": 0.92, "rationale": f"{len(rows)} identical postings of {m['ref']}; the other ledger has one"}
            return {"classification": "BREAK", "confidence": 0.5, "rationale": "repeated reference with differing amounts/dates"}
        if k == "amount_mismatch":
            diff = abs(m["core"][0]["amount"] - m["settlement"][0]["amount"])
            if diff <= cfg.recon_rounding_tolerance: return {"classification": "ROUNDING", "confidence": 0.93, "rationale": f"difference INR {diff:.2f} within tolerance INR {cfg.recon_rounding_tolerance:.2f}"}
            return {"classification": "BREAK", "confidence": 0.6, "rationale": f"difference INR {diff:,.2f} exceeds rounding tolerance"}
        row = (m["core"] or m["settlement"])[0]
        if k == "missing_in_settlement" and datetime.strptime(row["value_date"], "%Y-%m-%d").date() >= today:
            return {"classification": "TIMING", "confidence": 0.88, "rationale": f"value date {row['value_date']} is on/after batch date {today}; settlement expected T+1"}
        return {"classification": "BREAK", "confidence": 0.55, "rationale": f"{k.replace('_', ' ')}; posted {row['date']}, value date {row['value_date']} already past"}

    def search_related_entries(self, batch_id, mismatch_id):
        m = self.repo.mismatch(batch_id, mismatch_id)
        if not m: return {"error": f"unknown mismatch {mismatch_id}"}
        row = (m["core"] or m["settlement"])[0]; found = []
        if m["kind"] in ("missing_in_core", "missing_in_settlement"):
            for o in self.repo.mismatches(batch_id):
                if o["mismatch_id"] != mismatch_id and o["kind"] in ("missing_in_core", "missing_in_settlement") and o["kind"] != m["kind"]:
                    orow = (o["core"] or o["settlement"])[0]
                    if abs(orow["amount"] - row["amount"]) < 1e-9 and orow["date"] == row["date"]:
                        found.append({"ref": orow["ref"], "normalised_equal": norm_ref(orow["ref"]) == norm_ref(row["ref"]), "mismatch_id": o["mismatch_id"], "amount": orow["amount"]})
        hint = None
        if m["kind"] == "amount_mismatch":
            a, b = f"{m['core'][0]['amount']:.2f}", f"{m['settlement'][0]['amount']:.2f}"
            if a != b and sorted(a) == sorted(b): hint = f"digits of {a} and {b} are identical but transposed — likely keying error at source"
        return {"counterparts": found, "transposition_hint": hint}


# ---------------------------------------------------------------- schemas (drive Gateway targets, prompts and dispatch)
S, N = "string", "number"
def _p(**kw): return {k: {"type": t, "description": d} for k, (t, d) in kw.items()}

TOOL_SPECS = {
    "fraud": {
        "get_transaction_history": ("Baseline stats of the customer's last N days of transactions before a timestamp (count, mean, std, max, merchants, countries).", _p(customer_id=(S, "Customer id"), before_ts=(S, "ISO timestamp of the flagged transaction"), days=(N, "Look-back days (default 90)")), ["customer_id", "before_ts"]),
        "check_merchant_risk": ("Merchant reputation: risk_score 0-100, chargeback rate, merchant age.", _p(merchant_id=(S, "Merchant id")), ["merchant_id"]),
        "check_device_geo_consistency": ("Is the device known? How far from home? Foreign? Impossible travel vs the customer's last transaction?", _p(customer_id=(S, "Customer id"), device_id=(S, "Device id"), lat=(N, "Latitude"), lon=(N, "Longitude"), country=(S, "ISO country code"), ts=(S, "ISO timestamp")), ["customer_id", "device_id", "lat", "lon", "country", "ts"]),
        "get_dispute_history": ("Prior disputes on the account and how many were confirmed fraud.", _p(customer_id=(S, "Customer id")), ["customer_id"]),
        "get_related_transactions": ("Velocity: transactions in the window before ts. Use when the picture is ambiguous.", _p(customer_id=(S, "Customer id"), ts=(S, "ISO timestamp"), window_hours=(N, "Window hours (default 24)")), ["customer_id", "ts"]),
    },
    "kyc": {
        "extract_document_fields": ("OCR the application PDF (Amazon Textract) and store the fields. strategy='ocr_strict' repairs characters misread in the PAN (use it if PAN fails format check).", _p(doc_id=(S, "Application id, e.g. LD-007"), strategy=(S, "'default' or 'ocr_strict'")), ["doc_id"]),
        "lookup_customer_record": ("Fetch the core-banking customer record.", _p(customer_id=(S, "Customer id")), ["customer_id"]),
        "find_customer_by_pan": ("Which customer(s) own this PAN, and is it on the fraud blacklist?", _p(pan=(S, "PAN number")), ["pan"]),
        "cross_check_fields": ("Compare the stored extracted fields of doc_id with the customer record.", _p(doc_id=(S, "Application id"), customer_id=(S, "Customer id to compare against")), ["doc_id", "customer_id"]),
        "check_policy_rules": ("Evaluate policy rules (format, blacklist, ID validity, income, loan cap) on the stored fields and retrieve cited policy clauses from the knowledge base.", _p(doc_id=(S, "Application id")), ["doc_id"]),
    },
    "recon": {
        "classify_mismatch": ("Classify a ledger mismatch: TIMING | DUPLICATE | ROUNDING | BREAK with confidence and rationale.", _p(batch_id=(S, "Batch id"), mismatch_id=(S, "Mismatch id")), ["batch_id", "mismatch_id"]),
        "search_related_entries": ("Look for the counterpart of a mismatch under another reference format, or a digit transposition. Use when classification is BREAK or low-confidence.", _p(batch_id=(S, "Batch id"), mismatch_id=(S, "Mismatch id")), ["batch_id", "mismatch_id"]),
    },
}
TOOL_GROUP = {t: g for g, ts in TOOL_SPECS.items() for t in ts}


def dispatch(tools: Tools, tool_name, args):
    if tool_name not in TOOL_GROUP: return {"error": f"unknown tool {tool_name}"}
    try: return getattr(tools, tool_name)(**args)
    except Exception as e: return {"error": f"{type(e).__name__}: {e}"}     # tool failure is an observation, not a crash
