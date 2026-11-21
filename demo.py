"""Replay the documented example via the application HTTP API in a temporary database."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen

from app import make_server


def replay(port):
    def api(path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(f"http://127.0.0.1:{port}/api/{path}", data=data,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def plan(scenario):
        return api("plan", {"scenario": scenario, "deadline_hours": 48, "budget": 2400000})

    normal = plan("normal")["result"]
    print(f"正常班次 | 目标 {normal['target']} 件 | 预计运费 {normal['proposal']['cost']:,} IDR")
    for line in normal["proposal"]["shipments"]:
        print(f"  {line['lane_id']} | {line['quantity']} 件 | {line['hours']} 小时")
    print(f"到货前预计最大缺货 {normal['service_risk']['peak_shortage']} 件")
    print("低价基线复核 | " + " / ".join(i["detail"] for i in normal["baseline_review"]))
    partial = plan("disruption")
    proposal = partial["result"]["proposal"]
    print(f"线路停运 | 可调 {proposal['quantity']} 件 | 计划缺口 {proposal['uncovered']} 件")
    identifier = partial["id"]
    api("decision", {"id": identifier, "decision": "approved", "note": "余下 20 件另行补货"})
    line = {"id": identifier, "lane_id": proposal["shipments"][0]["lane_id"]}
    api("fulfillment", {**line, "action": "dispatch"})
    api("fulfillment", {**line, "action": "receive", "quantity": 11, "note": "1 件包装破损，转补货"})
    record = next(row for row in api("history") if row["id"] == identifier)
    view = record["execution"]
    print(f"签收对账 | 计划发 {proposal['quantity']} 件 | 实收 {view['received']} 件 | 最终缺口 {view['final_gap']} 件")
    print("过期快照 | " + plan("stale")["result"]["reason"])


if __name__ == "__main__":
    with TemporaryDirectory() as folder, make_server(0, Path(folder) / "demo.sqlite3") as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            replay(server.server_port)
        finally:
            server.shutdown()
            worker.join()
