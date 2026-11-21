"""Inventory, policy retrieval, dispatch and review agents."""
from collections import Counter
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path
from statistics import mean, stdev
from time import perf_counter
import json

from rag import QUERIES, retrieve

DATA_DIR = Path(__file__).with_name("data")


def load_demo():
    return tuple(json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
                 for name in ("scenarios.json", "policies.json"))


def inventory_agent(warehouses, hours, z, protect_demand):
    states = {}
    for name, row in warehouses.items():
        demand = ceil(mean(row["history"]) * hours / 24)
        safety = ceil(z * stdev(row["history"]) * row["lead_days"] ** 0.5)
        available = row["on_hand"] - row["reserved"]
        states[name] = {
            "on_hand": row["on_hand"], "reserved": row["reserved"], "available": available,
            "daily_mean": mean(row["history"]), "future_demand": demand,
            "safety_stock": safety, "transferable": max(0, available - safety - (demand if protect_demand else 0)),
        }
    return states


def dispatch_agent(lanes, states, target, case, constrained=True):
    """Maximize coverage, then minimize total cost on direct unit-price lanes."""
    remaining, balance = target, case["budget"]
    shipments, candidates = [], []
    stock = {n: s["transferable"] if constrained else s["available"] for n, s in states.items()}
    for lane in sorted(lanes, key=lambda x: (x["unit_cost"], x["hours"], x["id"])):
        origin = lane["origin"]
        reasons = []
        if lane["id"] in case["closed"]:
            reasons.append("线路停运")
        if lane["hours"] > case["deadline_hours"]:
            reasons.append("超过到货时限")
        if states[origin]["transferable"] <= 0:
            reasons.append("调出仓无可调余量")
        limit = max(0, min(lane["capacity"], stock[origin]))
        if constrained:
            candidates.append({**lane, "reasons": reasons, "max_available": limit})
        if (constrained and reasons) or lane["id"] in case["closed"]:
            continue
        quantity = min(limit, remaining, balance // lane["unit_cost"])
        if not quantity:
            continue
        cost = quantity * lane["unit_cost"]
        eta = datetime.fromisoformat(case["as_of"]) + timedelta(hours=lane["hours"])
        shipments.append({
            "lane_id": lane["id"], "origin": origin, "destination": lane["destination"],
            "quantity": quantity, "cost": cost, "hours": lane["hours"], "eta": eta.isoformat(),
        })
        stock[origin] -= quantity
        remaining -= quantity
        balance -= cost
    return {"shipments": shipments, "candidates": candidates, "uncovered": remaining,
            "quantity": target - remaining, "cost": case["budget"] - balance}


def review_agent(plan, lanes, states, target, case, evidence):
    """Replay all hard checks against source lanes and inventory."""
    issues, by_origin, by_lane = [], Counter(), Counter()
    index = {lane["id"]: lane for lane in lanes}
    cost = 0

    def add(rule, detail):
        issues.append({"rule": rule, "source": evidence[rule]["id"], "detail": detail})

    for row in plan["shipments"]:
        lane = index.get(row["lane_id"])
        quantity = row["quantity"]
        if lane is None or type(quantity) is not int or quantity <= 0:
            add("budget", "线路或调拨件数无效")
            continue
        origin = lane["origin"]
        if row["origin"] != origin or row["destination"] != lane["destination"]:
            add("arrival", "调拨地点与线路不符")
        expected_eta = datetime.fromisoformat(case["as_of"]) + timedelta(hours=lane["hours"])
        if row.get("hours") != lane["hours"] or row.get("eta") != expected_eta.isoformat():
            add("arrival", f"{lane['id']} 展示的时长或到货时间与线路快照不符")
        if row.get("cost") != quantity * lane["unit_cost"]:
            add("budget", f"{lane['id']} 明细运费与件数、单价不符")
        by_origin[origin] += quantity
        by_lane[lane["id"]] += quantity
        cost += quantity * lane["unit_cost"]
        if lane["id"] in case["closed"] or lane["hours"] > case["deadline_hours"]:
            add("arrival", f"{lane['id']} 停运或无法在截止前到货")
        if by_lane[lane["id"]] > lane["capacity"]:
            add("budget", f"{lane['id']} 超过线路余量")
    for origin, sent in by_origin.items():
        if sent > states[origin]["transferable"]:
            add("donor_reserve", f"{origin} 调出 {sent} 件，高于可调的 {states[origin]['transferable']} 件")
    total = sum(by_origin.values())
    if (cost != plan["cost"] or cost > case["budget"] or total > target
            or total != plan["quantity"] or plan["uncovered"] != target - total):
        add("budget", "费用或数量与约束、汇总不一致")
    return issues



def projected_shortage(shipments, receiver, deadline_hours):
    """Measure the largest estimated unmet demand before a shipment or the deadline."""
    arrivals = Counter()
    for row in shipments:
        if row["hours"] <= deadline_hours:
            arrivals[row["hours"]] += row["quantity"]
    supply, worst = receiver["available"], 0
    timeline = []
    for hour in sorted(set(arrivals) | {deadline_hours}):
        demand = ceil(receiver["daily_mean"] * hour / 24)
        shortage = max(0, demand - supply)
        worst = max(worst, shortage)
        supply += arrivals[hour]
        timeline.append({"hour": hour, "demand": demand, "arriving": arrivals[hour],
                         "shortage_before_arrival": shortage, "balance_after_arrival": supply - demand})
    return {"peak_shortage": worst, "timeline": timeline}


def run_case(data, policies, scenario_id, deadline_hours=None, budget=None, narrate=None):
    case = dict(data["scenarios"][scenario_id])
    if deadline_hours is not None:
        case["deadline_hours"] = deadline_hours
    if budget is not None:
        case["budget"] = budget
    destination = "MAKASSAR"
    trace = []

    def step(name, action):
        start = perf_counter()
        result = action()
        trace.append({"step": name, "ms": round((perf_counter() - start) * 1000, 2)})
        return result

    evidence = step("规则检索", lambda: retrieve(policies, destination, case["as_of"]))
    result = {
        "scenario": case, "sku": data["sku"], "sku_name": data["sku_name"],
        "warehouses": data["warehouse_names"], "trace": trace, "evidence": list(evidence.values()),
    }
    missing = set(QUERIES) - evidence.keys()
    if missing:
        return {**result, "status": "blocked", "reason": "缺少适用条款：" + "、".join(sorted(missing))}
    max_age = evidence["freshness"]["params"]["max_age_hours"]
    if case["snapshot_age"] > max_age:
        return {**result, "status": "blocked",
                "reason": f"库存快照已有 {case['snapshot_age']} 小时，超过 {max_age} 小时上限。"}

    policy = evidence["donor_reserve"]["params"]
    states = step("库存诊断", lambda: inventory_agent(
        data["warehouses"], case["deadline_hours"], policy["z"], policy["protect_demand"]
    ))
    receiver = states[destination]
    target = max(0, receiver["future_demand"] + receiver["safety_stock"] - receiver["available"])
    lanes = [row for row in data["lanes"] if row["destination"] == destination]
    plan = step("调拨规划", lambda: dispatch_agent(lanes, states, target, case))
    baseline = dispatch_agent(lanes, states, target, case, constrained=False)
    issues, baseline_issues = step("独立复核", lambda: (
        review_agent(plan, lanes, states, target, case, evidence),
        review_agent(baseline, lanes, states, target, case, evidence),
    ))
    if issues:
        status = "blocked"
    elif target == 0:
        status = "no_action"
    elif plan["quantity"] == 0:
        status = "infeasible"
    else:
        status = "partial" if plan["uncovered"] else "ready"

    result.update(status=status, target=target, inventory=states, proposal=plan, baseline=baseline,
                  review=issues, baseline_review=baseline_issues,
                  reason="复核发现违规，请查看规则证据。" if issues else "")
    result["service_risk"] = projected_shortage(plan["shipments"], receiver, case["deadline_hours"])
    if narrate:
        result["model_notes"] = step("模型分析与审阅", lambda: narrate(result))
    return result
