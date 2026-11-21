"""Local demonstration API and UI for inventory transfer planning."""
from argparse import ArgumentParser
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from model import narrate
from fulfillment import execution_view, record_event, setup_events
from planning import load_demo, run_case

ROOT = Path(__file__).parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def setup(path):
    with closing(open_db(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS proposals (
            id TEXT PRIMARY KEY, created TEXT NOT NULL, scenario TEXT NOT NULL, result TEXT NOT NULL,
            decision TEXT NOT NULL DEFAULT 'pending', note TEXT, decided TEXT
        )""")
        setup_events(db)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class API(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_bytes(self, status, payload, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, status, value):
        self.send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in STATIC:
            file, mime = STATIC[self.path]
            self.send_bytes(200, (ROOT / "static" / file).read_bytes(), mime)
        elif self.path == "/api/scenarios":
            self.send_json(200, {"scenarios": self.server.data["scenarios"], "live": self.server.live})
        elif self.path == "/api/history":
            with closing(open_db(self.server.db)) as db:
                db.execute("BEGIN")
                rows = db.execute("SELECT * FROM proposals ORDER BY created DESC, rowid DESC LIMIT 20")
                records = []
                for row in rows:
                    result = json.loads(row["result"])
                    execution = execution_view(db, row["id"], row["decision"], result)
                    records.append({**dict(row), "result": result, "execution": execution})
            self.send_json(200, records)
        else:
            self.send_json(404, {"error": "找不到该页面"})

    def do_POST(self):
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError()
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 12000:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            self.send_json(400, {"error": "请求格式无效"})
            return

        if self.path == "/api/plan":
            self.plan(body)
        elif self.path == "/api/decision":
            self.decide(body)
        elif self.path == "/api/fulfillment":
            with closing(open_db(self.server.db)) as db, db:
                status, response = record_event(db, body, now())
            self.send_json(status, response)
        else:
            self.send_json(404, {"error": "找不到该接口"})

    def plan(self, body):
        scenario = body.get("scenario")
        hours, budget = body.get("deadline_hours"), body.get("budget")
        if (not isinstance(scenario, str) or scenario not in self.server.data["scenarios"]
                or type(hours) is not int or not 1 <= hours <= 168
                or type(budget) is not int or not 0 <= budget <= 1_000_000_000):
            self.send_json(400, {"error": "请选择场景，填写 1–168 小时和有效的整数运费预算"})
            return
        result = run_case(self.server.data, self.server.policies, scenario, hours, budget,
                          narrate=narrate if self.server.live else None)
        identifier = str(uuid4())
        with closing(open_db(self.server.db)) as db, db:
            db.execute("INSERT INTO proposals (id, created, scenario, result) VALUES (?, ?, ?, ?)",
                       (identifier, now(), scenario, json.dumps(result, ensure_ascii=False)))
        self.send_json(201, {"id": identifier, "decision": "pending", "result": result})

    def decide(self, body):
        identifier, decision, note = body.get("id"), body.get("decision"), body.get("note", "")
        if not isinstance(identifier, str) or decision not in ("approved", "rejected") or not isinstance(note, str) or len(note) > 500:
            self.send_json(400, {"error": "审批参数无效"})
            return
        note = note.strip()
        with closing(open_db(self.server.db)) as db, db:
            row = db.execute("SELECT result, decision FROM proposals WHERE id = ?", (identifier,)).fetchone()
            if row is None:
                self.send_json(404, {"error": "方案不存在"})
                return
            if row["decision"] != "pending":
                self.send_json(409, {"error": "该方案已审批"})
                return
            result = json.loads(row["result"])
            status = result["status"]
            if decision == "approved" and (status not in ("ready", "partial") or status == "partial" and not note):
                self.send_json(422, {"error": "仅可批准有效方案，部分调拨需填写缺口处理说明"})
                return
            updated = db.execute(
                "UPDATE proposals SET decision=?, note=?, decided=? WHERE id=? AND decision='pending'",
                (decision, note, now(), identifier),
            ).rowcount
            execution = execution_view(db, identifier, decision, result) if updated else None
        if not updated:
            self.send_json(409, {"error": "该方案已审批"})
            return
        self.send_json(200, {"id": identifier, "decision": decision, "note": note, "execution": execution})


def make_server(port, db, live=False):
    setup(db)
    server = ThreadingHTTPServer(("127.0.0.1", port), API)
    server.data, server.policies = load_demo()
    server.db, server.live = db, live
    return server


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--port", type=int, default=8077)
    parser.add_argument("--live", action="store_true", help="Enable DeepSeek analysis and review.")
    parser.add_argument("--db", type=Path, default=ROOT / "plans.sqlite3")
    args = parser.parse_args()
    if args.live and not os.getenv("DEEPSEEK_API_KEY"):
        parser.error("Set DEEPSEEK_API_KEY before using --live.")
    with make_server(args.port, args.db, args.live) as server:
        print(f"Open http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
