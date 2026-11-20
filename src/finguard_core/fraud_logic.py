"""Deterministic fraud scoring. Used as an independent, code-side baseline to VERIFY the model's proposal."""
import math
from datetime import datetime

FMT = "%Y-%m-%dT%H:%M:%S"
parse = lambda s: datetime.strptime(s[:19], FMT)
WEIGHTS = {"get_transaction_history": .35, "check_device_geo_consistency": .25, "check_merchant_risk": .25, "get_dispute_history": .15}


def haversine(lat1, lon1, lat2, lon2):
    la1, lo1, la2, lo2 = map(math.radians, (lat1, lon1, lat2, lon2))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def score(event, ev):
    pts, why = 0, []
    def add(p, t):
        nonlocal pts; pts += p; why.append(f"{p:+d}: {t}")
    h = ev.get("get_transaction_history")
    if h is not None:
        if h["count"] < 3: add(10, f"no usable spending baseline ({h['count']} prior transactions)")
        else:
            z = (event["amount"] - h["mean"]) / max(h["std"], 0.25 * h["mean"])
            if z >= 4: add(30, f"amount {event['amount']:,.0f} is {z:.1f} std-devs above mean {h['mean']:,.0f}")
            elif z >= 2.5: add(20, f"amount is {z:.1f} std-devs above normal")
            elif z >= 1.5: add(8, f"amount is mildly high ({z:.1f} std-devs)")
            if event["amount"] > 2 * h["max"]: add(10, f"more than 2x the customer's largest prior transaction ({h['max']:,.0f})")
            if event["merchant_id"] not in h["merchant_ids"]: add(5, "merchant never used before by this customer")
    m = ev.get("check_merchant_risk")
    if m is not None:
        if m["risk_score"] >= 70: add(25, f"high-risk merchant (score {m['risk_score']})")
        elif m["risk_score"] >= 40: add(12, f"medium-risk merchant (score {m['risk_score']})")
    d = ev.get("check_device_geo_consistency")
    if d is not None:
        if not d["known_device"]: add(12, "device not previously seen on this account")
        if d["foreign"]: add(15, f"transaction originates abroad ({d['country']})")
        elif d["distance_from_home_km"] > 500: add(8, f"{d['distance_from_home_km']} km from home")
        if d["impossible_travel"]: add(25, f"impossible travel (~{d.get('implied_speed_kmh')} km/h from last transaction)")
    p = ev.get("get_dispute_history")
    if p is not None:
        if p["fraud_confirmed"] >= 1: add(10, f"{p['fraud_confirmed']} prior fraud-confirmed dispute(s)")
        elif p["count"] >= 2: add(5, f"{p['count']} prior disputes")
    r = ev.get("get_related_transactions")
    if r is not None:
        if r["count"] >= 4 or r["max_same_merchant"] >= 4: add(15, f"velocity: {r['count']} transactions in {r['window_hours']}h")
        elif r["count"] >= 3: add(8, f"elevated velocity: {r['count']} transactions in {r['window_hours']}h")
    return min(pts, 100), why


def coverage(ev):
    return sum(w for t, w in WEIGHTS.items() if t in ev and not (t == "get_transaction_history" and ev[t]["count"] < 3))


def decision_for(risk, cfg):
    if risk <= cfg.fraud_auto_clear_max_risk: return "AUTO_CLEAR"
    if risk <= cfg.fraud_hold_max_risk: return "HOLD_AND_NOTIFY"
    if risk < cfg.fraud_block_min_risk: return "ESCALATE"
    return "AUTO_BLOCK"
