"""Synthetic data generator. EVERYTHING here is fabricated (names, PANs, accounts, merchants).
No real personal or financial data is used. Seeded for reproducibility."""
import csv
import json
import math
import random
from datetime import datetime, timedelta

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = DATA_DIR / "docs"
REF_NOW = datetime(2026, 10, 12, 10, 0, 0)

rnd = random.Random(42)
FIRST = ["Aarav", "Vivaan", "Aditya", "Rohan", "Karan", "Neha", "Priya", "Ananya", "Sneha", "Pooja",
         "Rahul", "Amit", "Sunita", "Meera", "Vikram", "Deepa", "Arjun", "Kavya", "Manish", "Ritu"]
LAST = ["Sharma", "Verma", "Das", "Sarkar", "Iyer", "Nair", "Patel", "Gupta", "Singh", "Reddy",
        "Mehta", "Banerjee", "Joshi", "Kulkarni", "Chatterjee", "Mishra", "Yadav", "Pillai"]
CITIES = {
    "Mumbai": (19.07, 72.88), "Delhi": (28.61, 77.21), "Bengaluru": (12.97, 77.59),
    "Chennai": (13.08, 80.27), "Kolkata": (22.57, 88.36), "Hyderabad": (17.38, 78.48),
    "Pune": (18.52, 73.86), "Patna": (25.59, 85.14),
}
FOREIGN = {"Lagos": ("NG", 6.52, 3.38), "Bucharest": ("RO", 44.43, 26.10),
           "Singapore": ("SG", 1.35, 103.82), "London": ("GB", 51.51, -0.13)}
CATS = ["grocery", "fuel", "dining", "utilities", "pharmacy", "electronics", "travel", "fashion"]
STREETS = ["MG Road", "Park Street", "Station Road", "Lake View Lane", "Residency Road", "Nehru Marg"]


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def pan_for(first, last, used):
    while True:
        p = "".join(rnd.choices("ABCDEFGHJKLMNPRSTUVWXYZ", k=3)) + "P" + last[0].upper() \
            + f"{rnd.randint(1000, 9999)}" + rnd.choice("ABCDEFGHJKLMNPRSTUVWXYZ")
        if p not in used:
            used.add(p)
            return p


def make_customers(n=60):
    used, out = set(), []
    for i in range(1, n + 1):
        f, l = rnd.choice(FIRST), rnd.choice(LAST)
        city = rnd.choice(list(CITIES))
        lat, lon = CITIES[city]
        income = rnd.choice([30, 40, 55, 70, 90, 120, 160]) * 1000
        cid = f"C{i:04d}"
        out.append({
            "customer_id": cid, "name": f"{f} {l}", "dob": f"{rnd.randint(1965, 2000)}-{rnd.randint(1,12):02d}-{rnd.randint(1,28):02d}",
            "pan": pan_for(f, l, used),
            "address": f"{rnd.randint(1, 220)}, {rnd.choice(STREETS)}, {city} {rnd.randint(100000, 999999)}",
            "phone": f"+91-9{rnd.randint(100000000, 999999999)}", "employer": rnd.choice(["Infosys", "TCS", "Wipro", "HDFC", "Self-employed", "Tata Motors", "Govt. Teacher"]),
            "monthly_income": income, "account_open_date": f"{rnd.randint(2012, 2025)}-0{rnd.randint(1,9)}-1{rnd.randint(0,9)}",
            "accounts": [{"acct_no": f"SB{rnd.randint(10**9, 10**10-1)}", "type": "savings", "balance": rnd.randint(20, 900) * 1000}],
            "known_devices": [f"D-{cid}-1"] + ([f"D-{cid}-2"] if rnd.random() < .4 else []),
            "home_geo": {"city": city, "country": "IN", "lat": lat, "lon": lon},
            "avg_txn": round(income * rnd.uniform(0.04, 0.10), -1), "usual_merchants": [], "kyc_status": "VERIFIED",
        })
    # C0001: a high earner who legitimately makes big transactions (for the high-value guardrail demo)
    c = out[0]; c.update(monthly_income=400_000, avg_txn=95_000, employer="Self-employed")
    # C0060: a brand-new customer with almost no history (for the low-confidence guardrail demo)
    c = out[-1]; c.update(account_open_date="2026-10-05", avg_txn=4_000)
    return out


def make_merchants():
    ms = []
    for i in range(1, 41):
        tier = "low" if i <= 28 else "medium" if i <= 34 else "high"
        ms.append({"merchant_id": f"M{i:04d}", "name": f"{rnd.choice(['Sri','New','Royal','City','Metro','Global'])} {rnd.choice(['Mart','Traders','Pay','Stores','Exports','Hub'])} {i}",
                   "category": rnd.choice(CATS),
                   "risk_score": {"low": rnd.randint(3, 22), "medium": rnd.randint(42, 60), "high": rnd.randint(76, 95)}[tier],
                   "chargeback_rate": {"low": 0.002, "medium": 0.02, "high": 0.09}[tier],
                   "age_days": {"low": rnd.randint(700, 4000), "medium": rnd.randint(120, 400), "high": rnd.randint(8, 60)}[tier],
                   "tier": tier})
    return ms


def jitter(geo):
    return {"city": geo["city"], "country": geo["country"], "lat": round(geo["lat"] + rnd.uniform(-.05, .05), 4),
            "lon": round(geo["lon"] + rnd.uniform(-.05, .05), 4)}


class TxnFactory:
    def __init__(self, customers, merchants):
        self.customers, self.merchants, self.n = customers, merchants, 0
        self.low = [m["merchant_id"] for m in merchants if m["tier"] == "low"]
        self.med = [m["merchant_id"] for m in merchants if m["tier"] == "medium"]
        self.high = [m["merchant_id"] for m in merchants if m["tier"] == "high"]
        for c in customers:
            c["usual_merchants"] = rnd.sample(self.low, 5)
        self.all = []

    def make(self, c, ts, amount, merchant_id=None, device=None, geo=None, flagged=False, rule=None, tag="history"):
        self.n += 1
        t = {"txn_id": f"T{self.n:05d}", "customer_id": c["customer_id"], "ts": iso(ts), "amount": round(amount, 2),
             "merchant_id": merchant_id or rnd.choice(c["usual_merchants"]),
             "device_id": device or c["known_devices"][0], "geo": geo or jitter(c["home_geo"]),
             "flagged": flagged, "rule_triggered": rule, "tag": tag}
        self.all.append(t)
        return t

    def normal_amt(self, c):
        return max(50, rnd.lognormvariate(math.log(c["avg_txn"]), 0.35))

    def history(self, c, n):
        for _ in range(n):
            self.make(c, REF_NOW - timedelta(days=rnd.uniform(4, 88), hours=rnd.uniform(0, 12)), self.normal_amt(c))


def make_transactions(customers, merchants):
    f = TxnFactory(customers, merchants)
    cust = {c["customer_id"]: c for c in customers}
    for c in customers:
        f.history(c, 1 if c["customer_id"] == "C0060" else 15)

    sc = {}   # scenario name -> txn_id
    pool = [c for c in customers if c["customer_id"] not in ("C0001", "C0060")]
    ev_ts = lambda: REF_NOW - timedelta(hours=rnd.uniform(1, 70))

    # --- 162 normal (unflagged) events
    for _ in range(162):
        c = rnd.choice(customers[:-1]); f.make(c, ev_ts(), f.normal_amt(c), tag="normal")
    # --- 8 false-positive flagged (look normal)
    for i in range(8):
        c = pool[i]; t = f.make(c, ev_ts(), c["avg_txn"] * rnd.uniform(.8, 1.3), flagged=True,
                                rule=rnd.choice(["NIGHT_TIME", "ROUND_AMOUNT"]), tag="false_positive")
        if i == 0: sc["clear_ok"] = t["txn_id"]
    # --- special: high-value but perfectly normal for this customer (G3 should fire)
    c = cust["C0001"]
    sc["clear_but_highvalue"] = f.make(c, ev_ts(), 110_000, flagged=True, rule="HIGH_VALUE", tag="false_positive")["txn_id"]
    # --- special: brand-new customer, unknown merchant, thin history (G1 should fire)
    c = cust["C0060"]
    sc["low_confidence"] = f.make(c, ev_ts(), 3_500, merchant_id="M9999", flagged=True, rule="NEW_CUSTOMER", tag="thin_history")["txn_id"]

    def foreign(c):
        city = rnd.choice(list(FOREIGN)); cc, la, lo = FOREIGN[city]
        return {"city": city, "country": cc, "lat": la, "lon": lo}

    def prior_home_txn(c, hours_before, ts):   # makes 'impossible travel' detectable
        f.make(c, ts - timedelta(hours=hours_before), f.normal_amt(c), tag="history")

    # a) 8 high_amount (home geo, known device)
    for i in range(8):
        c = pool[10 + i]
        t = f.make(c, ev_ts(), c["avg_txn"] * rnd.uniform(7, 12), flagged=True, rule="HIGH_AMOUNT", tag="anom_high_amount")
        if i == 0: sc["hold_medium"] = t["txn_id"]
    # b) 7 odd_geo (foreign + new device + impossible travel)
    for i in range(7):
        c = pool[20 + i]; ts = ev_ts(); prior_home_txn(c, rnd.uniform(1, 3), ts)
        f.make(c, ts, c["avg_txn"] * rnd.uniform(1.5, 3), device=f"D-NEW-{i:03d}", geo=foreign(c), flagged=True,
               rule="IMPOSSIBLE_TRAVEL", tag="anom_geo")
    # c) 6 risky merchant
    for i in range(6):
        c = pool[27 + i]
        f.make(c, ev_ts(), c["avg_txn"] * rnd.uniform(2, 4), merchant_id=rnd.choice(f.high), flagged=True,
               rule="MERCHANT_RISK", tag="anom_merchant")
    # d) 5 severe combos: 2 under the auto-block value limit, 3 over it
    for i in range(5):
        c = pool[34 + i] if i < 2 else (pool[34 + i] if i < 4 else cust["C0001"]); ts = ev_ts(); prior_home_txn(c, 2, ts)
        amt = [28_000, 45_000, 190_000, 320_000, 780_000][i]
        if i < 2: c = dict(c, avg_txn=3_000)  # (amounts above are anomalous relative to a modest baseline)
        t = f.make(cust[c["customer_id"]], ts, amt, merchant_id=rnd.choice(f.high), device=f"D-NEW-9{i}", geo=foreign(c),
                   flagged=True, rule="IMPOSSIBLE_TRAVEL", tag="anom_severe")
        if i == 0: sc["block_lowvalue"] = t["txn_id"]
        if i == 2: sc["block_highvalue"] = t["txn_id"]
    # e) 4 velocity bursts toward medium-risk merchants
    for i in range(4):
        c = pool[40 + i]; ts = ev_ts(); m = rnd.choice(f.med)
        for k in range(5):
            f.make(c, ts - timedelta(hours=rnd.uniform(.2, 8)), c["avg_txn"] * rnd.uniform(1, 1.8), merchant_id=m, tag="burst")
        f.make(c, ts, c["avg_txn"] * rnd.uniform(1.5, 2.5), merchant_id=m, flagged=True, rule="VELOCITY", tag="anom_velocity")
    f.all.sort(key=lambda t: t["ts"])
    return f.all, sc


def make_disputes(customers):
    d = []
    for i, c in enumerate(rnd.sample(customers, 18)):
        d.append({"dispute_id": f"DSP{i:04d}", "customer_id": c["customer_id"], "date": iso(REF_NOW - timedelta(days=rnd.randint(10, 300))),
                  "reason": rnd.choice(["unauthorized transaction", "merchant did not deliver", "duplicate charge"]),
                  "fraud_confirmed": rnd.random() < .35})
    return d


POLICY = {
    "blacklisted_pans": ["BLKPX9999Z", "FRDPQ1234K", "SCMPR5678L"],
    "required_fields": ["full_name", "dob", "pan", "address", "monthly_income", "loan_amount", "id_expiry"],
    "clauses": [
        {"id": "KYC-1", "title": "PAN format", "text": "PAN must be 10 characters: five letters, four digits, one letter. Reject unreadable or malformed PAN numbers and request a clear copy."},
        {"id": "KYC-2", "title": "Blacklist", "text": "Applications presenting a PAN on the fraud blacklist must be escalated to the fraud team immediately; do not inform the applicant."},
        {"id": "KYC-3", "title": "ID validity", "text": "Photo ID must be valid on the application date. Expired identity documents are not acceptable; request a current ID."},
        {"id": "KYC-4", "title": "Customer record match", "text": "Name, date of birth and PAN on the application must match the existing customer record. A PAN registered to a different customer indicates possible forgery and must be escalated."},
        {"id": "LND-1", "title": "Income eligibility", "text": "Minimum verified monthly income is INR 25,000 for any loan application."},
        {"id": "LND-2", "title": "Loan-to-income cap", "text": "Requested loan amount must not exceed five times verified annual income."},
        {"id": "LND-3", "title": "Large loans", "text": "Loans above INR 50,00,000 require senior underwriter review regardless of automated checks."},
        {"id": "LND-4", "title": "Income consistency", "text": "Stated income more than 50 percent above the income on record must be supported by fresh salary proof before processing."},
    ],
}


def make_docs(customers, sc):
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    cust = {c["customer_id"]: c for c in customers}
    plan = [("clean", 6), ("pan_fixable", 2), ("pan_unfixable", 1), ("blacklisted", 1), ("pan_other_customer", 1),
            ("expired_id", 2), ("missing_income", 1), ("loan_exceeds_income", 2), ("address_mismatch", 1),
            ("name_typo", 1), ("income_inflated", 1), ("high_value_clean", 1)]
    docs, n, ids = [], 0, [c for c in customers if c["customer_id"] not in ("C0001", "C0060")]
    for kind, count in plan:
        for k in range(count):
            n += 1
            c = cust["C0001"] if kind == "high_value_clean" else ids[n]
            d = {"Application ID": f"LD-{n:03d}", "Customer ID": c["customer_id"], "Full Name": c["name"], "Date of Birth": c["dob"],
                 "PAN": c["pan"], "Address": c["address"], "Employer": c["employer"], "Monthly Income (INR)": c["monthly_income"],
                 "Loan Amount Requested (INR)": c["monthly_income"] * rnd.randint(10, 35), "Loan Purpose": rnd.choice(["Home renovation", "Vehicle", "Education", "Personal"]),
                 "Photo ID": f"DL-{rnd.randint(10**8, 10**9-1)}", "Photo ID Expiry": f"{rnd.randint(2029, 2034)}-0{rnd.randint(1,9)}-15"}
            if kind == "pan_fixable":
                p = c["pan"]; d["PAN"] = p[:5] + p[5:9].replace("1", "I", 1).replace("0", "O", 1) + p[9] if any(ch in p[5:9] for ch in "10") else p[:5] + "O" + p[6:]
                if d["PAN"] == p: d["PAN"] = p[:5] + "O" + p[6:]
            elif kind == "pan_unfixable": d["PAN"] = c["pan"][:8]
            elif kind == "blacklisted": d["PAN"] = POLICY["blacklisted_pans"][0]
            elif kind == "pan_other_customer": d["PAN"] = cust[ids[n + 20]["customer_id"]]["pan"]
            elif kind == "expired_id": d["Photo ID Expiry"] = "2025-03-15"
            elif kind == "missing_income": del d["Monthly Income (INR)"]
            elif kind == "loan_exceeds_income": d["Loan Amount Requested (INR)"] = c["monthly_income"] * 90
            elif kind == "address_mismatch": d["Address"] = f"7, {rnd.choice(STREETS)}, {rnd.choice(list(CITIES))} 400001"
            elif kind == "name_typo": d["Full Name"] = c["name"] + "a"
            elif kind == "income_inflated": d["Monthly Income (INR)"] = int(c["monthly_income"] * 2.2); d["Loan Amount Requested (INR)"] = c["monthly_income"] * 20
            elif kind == "high_value_clean": d["Loan Amount Requested (INR)"] = 6_000_000
            path = DOCS_DIR / f"{d['Application ID']}.pdf"
            cv = canvas.Canvas(str(path), pagesize=A4); y = 800
            cv.setFont("Helvetica-Bold", 14); cv.drawString(60, y, "LOAN APPLICATION FORM (SYNTHETIC)"); y -= 34
            cv.setFont("Helvetica", 11)
            for kx, vx in d.items():
                cv.drawString(60, y, f"{kx}: {vx}"); y -= 22
            cv.save()
            docs.append({"doc_id": d["Application ID"], "file": f"docs/{path.name}", "customer_id": c["customer_id"], "scenario": kind})
            sc.setdefault("kyc", {}).setdefault(kind, d["Application ID"])
    return docs


def make_ledgers():
    """Core-banking export vs external settlement file with deliberate mismatches."""
    today = REF_NOW.date()
    core, sett, sc = [], [], {}
    def row(ref, amt, d, vd, rail="NEFT", cp=None, src="core"):
        return {"entry_id": f"{src[:1].upper()}-{ref}", "ref": ref, "date": str(d), "value_date": str(vd), "amount": f"{amt:.2f}",
                "currency": "INR", "rail": rail, "counterparty": cp or f"Merchant {rnd.randint(1, 40)}", "source": src}
    for i in range(1, 49):   # 48 clean matches
        amt = round(rnd.uniform(500, 90_000), 2); d = today - timedelta(days=rnd.randint(1, 3))
        cp = f"Merchant {rnd.randint(1, 40)}"
        core.append(row(f"UTR{i:06d}", amt, d, d, cp=cp)); sett.append(row(f"UTR{i:06d}", amt, d, d, cp=cp, src="settlement"))
    # timing differences: in core, not yet in settlement (value date today / tomorrow)
    for i, amt in zip(range(100, 104), [12_500.00, 48_200.50, 9_999.00, 3_100_000.00]):
        core.append(row(f"UTR{i:06d}", amt, today, today + timedelta(days=1), rail="NEFT")); sc.setdefault("timing", []).append(f"UTR{i:06d}")
    # duplicates (same ref posted twice in core)
    for i, amt in zip(range(110, 112), [22_000.00, 7_450.00]):
        d = today - timedelta(days=1)
        for _ in range(2): core.append(row(f"UTR{i:06d}", amt, d, d))
        sett.append(row(f"UTR{i:06d}", amt, d, d, src="settlement")); sc.setdefault("duplicate", []).append(f"UTR{i:06d}")
    # rounding differences
    for i, (a, b) in zip(range(120, 123), [(1250.50, 1250.49), (8800.00, 8800.75), (15_000.10, 15_000.00)]):
        d = today - timedelta(days=1)
        core.append(row(f"UTR{i:06d}", a, d, d)); sett.append(row(f"UTR{i:06d}", b, d, d, src="settlement")); sc.setdefault("rounding", []).append(f"UTR{i:06d}")
    # ref-format mismatch (same payment, different reference text) -> agent must discover the pair
    d = today - timedelta(days=1)
    core.append(row("UTR000130", 33_300.00, d, d, cp="Merchant 7")); sett.append(row("utr000130 ", 33_300.00, d, d, cp="Merchant 7", src="settlement")); sc["ref_format"] = "UTR000130"
    # genuine breaks: digit transposition + an orphan old settlement credit
    core.append(row("UTR000140", 125_000.00, d, d)); sett.append(row("UTR000140", 152_000.00, d, d, src="settlement")); sc.setdefault("break", []).append("UTR000140")
    old = today - timedelta(days=6)
    sett.append(row("UTR000141", 67_890.00, old, old, src="settlement")); sc["break"].append("UTR000141")
    for name, rows in (("ledger_core.csv", core), ("ledger_settlement.csv", sett)):
        with open(DATA_DIR / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    return sc


def generate():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    customers, merchants = make_customers(), make_merchants()
    txns, sc = make_transactions(customers, merchants)
    disputes = make_disputes(customers)
    sc = {"fraud": sc}
    docs = make_docs(customers, sc)
    sc["recon"] = make_ledgers()
    sc["kyc_docs"] = docs
    for name, obj in (("customers", customers), ("merchants", merchants), ("transactions", txns), ("disputes", disputes),
                      ("policy", POLICY), ("scenarios", sc)):
        (DATA_DIR / f"{name}.json").write_text(json.dumps(obj, indent=1))
    flagged = sum(t["flagged"] for t in txns)
    print(f"customers={len(customers)} merchants={len(merchants)} transactions={len(txns)} flagged={flagged} docs={len(docs)}")
    return sc


if __name__ == "__main__":
    generate()
