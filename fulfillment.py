"""Simulated dispatch and final receipt for the lines of an approved proposal."""
import json


def setup_events(db):
    db.execute("""CREATE TABLE IF NOT EXISTS shipment_events (
        proposal_id TEXT NOT NULL REFERENCES proposals(id), lane_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('dispatch', 'receive')),
        quantity INTEGER NOT NULL CHECK(quantity >= 0), note TEXT NOT NULL, recorded TEXT NOT NULL,
        PRIMARY KEY(proposal_id, lane_id, action)
    )""")


def execution_view(db, identifier, decision, result):
    if decision != "approved":
        return None
    records = db.execute(
        "SELECT lane_id, action, quantity, note, recorded FROM shipment_events "
        "WHERE proposal_id=? ORDER BY rowid", (identifier,),
    )
    events = [dict(row) for row in records]
    lines = {s["lane_id"]: {"lane_id": s["lane_id"], "planned": s["quantity"],
                            "state": "awaiting_dispatch", "received": None}
             for s in result["proposal"]["shipments"]}
    for event in events:
        line = lines[event["lane_id"]]
        if event["action"] == "dispatch":
            line["state"] = "in_transit"
        else:
            line.update(state="received", received=event["quantity"])
    received = sum(line["received"] or 0 for line in lines.values())
    complete = all(line["state"] == "received" for line in lines.values())
    return {"lines": list(lines.values()), "events": events, "received": received,
            "complete": complete, "final_gap": result["target"] - received if complete else None}


def record_event(db, body, timestamp):
    identifier, lane, action = (body.get(key) for key in ("id", "lane_id", "action"))
    note, quantity = body.get("note", ""), body.get("quantity")
    if (not isinstance(identifier, str) or not isinstance(lane, str)
            or action not in ("dispatch", "receive") or not isinstance(note, str) or len(note) > 500):
        return 400, {"error": "请提供有效方案、线路、操作和说明。"}
    note = note.strip()
    # Serialise the read/check/append so simultaneous registrations cannot both succeed.
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT decision, result FROM proposals WHERE id=?", (identifier,)).fetchone()
    if row is None:
        return 404, {"error": "方案不存在。"}
    if row["decision"] != "approved":
        return 409, {"error": "发运前须先批准方案。"}
    result = json.loads(row["result"])
    view = execution_view(db, identifier, row["decision"], result)
    line = next((item for item in view["lines"] if item["lane_id"] == lane), None)
    if line is None:
        return 422, {"error": "该线路不在获批方案中。"}
    if action == "dispatch":
        if line["state"] != "awaiting_dispatch":
            return 409, {"error": "该线路已登记发运。"}
        if quantity is not None and (type(quantity) is not int or quantity != line["planned"]):
            return 422, {"error": "按批准的线路件数整批发运。"}
        quantity = line["planned"]
    else:
        if line["state"] != "in_transit":
            return 409, {"error": "仅在途线路可登记一次最终签收。"}
        if type(quantity) is not int or not 0 <= quantity <= line["planned"]:
            return 422, {"error": "实收件数须为零至发运件数之间的整数。"}
        if quantity < line["planned"] and not note:
            return 422, {"error": "短收须填写原因或后续处理说明。"}
    db.execute("INSERT INTO shipment_events VALUES (?, ?, ?, ?, ?, ?)",
               (identifier, lane, action, quantity, note, timestamp))
    return 200, {"id": identifier, "execution": execution_view(db, identifier, "approved", result)}
