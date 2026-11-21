from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import json
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import make_server
from model import narrate
from planning import load_demo, review_agent, run_case


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.data, self.policies = load_demo()

    def test_default_plan_covers_need_and_baseline_arrives_late(self):
        r = run_case(self.data, self.policies, "normal")
        self.assertEqual((r["status"], r["target"], r["proposal"]["quantity"], r["proposal"]["cost"]),
                         ("ready", 32, 32, 2060000))
        self.assertEqual([x["lane_id"] for x in r["proposal"]["shipments"]], ["SBY-MKS", "BPN-MKS"])
        self.assertEqual(r["review"], [])
        self.assertEqual([x["source"] for x in r["baseline_review"]], ["OPS-02@1", "OPS-01@2"])
        self.assertEqual(r["service_risk"]["peak_shortage"], 5)

    def test_disruption_leaves_uncovered_units(self):
        r = run_case(self.data, self.policies, "disruption")
        self.assertEqual((r["status"], r["proposal"]["quantity"], r["proposal"]["uncovered"]), ("partial", 12, 20))
        self.assertNotIn("BPN-MKS", [row["lane_id"] for row in r["proposal"]["shipments"]])

    def test_review_recalculates_displayed_line_totals_eta_and_gap(self):
        r = run_case(self.data, self.policies, "normal")
        plan = deepcopy(r["proposal"])
        plan["shipments"][0].update(cost=1, hours=1, eta="incorrect")
        plan["uncovered"] += 1
        issues = review_agent(plan, self.data["lanes"], r["inventory"], r["target"], r["scenario"],
                              {p["rule"]: p for p in r["evidence"]})
        self.assertEqual([i["rule"] for i in issues], ["arrival", "budget", "budget"])

    def test_budget_ceiling(self):
        r = run_case(self.data, self.policies, "normal", budget=56000)
        self.assertEqual((r["proposal"]["quantity"], r["proposal"]["cost"]), (1, 55000))

    def test_zero_budget_and_short_window_have_distinct_outcomes(self):
        self.assertEqual(run_case(self.data, self.policies, "normal", budget=0)["status"], "infeasible")
        self.assertEqual(run_case(self.data, self.policies, "normal", deadline_hours=1)["status"], "no_action")

    def test_model_receives_evidence_and_reviewer_failure_preserves_plan(self):
        with patch("model.chat", side_effect=["按 OPS-02@1 核对时限。", TimeoutError()]) as chat:
            r = run_case(self.data, self.policies, "normal", narrate=narrate)
        analyst_context = chat.call_args_list[0].args[1]
        self.assertIn("OPS-02@1", {e["id"] for e in analyst_context["evidence"]})
        self.assertEqual(analyst_context["service_risk"]["peak_shortage"], 5)
        self.assertEqual(chat.call_args_list[1].args[1]["分析员解读"], r["model_notes"]["规划分析员"])
        self.assertIn("暂不可用", r["model_notes"]["审批审阅员"])
        self.assertEqual((r["status"], r["proposal"]["quantity"]), ("ready", 32))

    def test_retrieval_rejects_expired_and_wrong_scope(self):
        r = run_case(self.data, self.policies, "normal")
        ids = {row["id"] for row in r["evidence"]}
        self.assertEqual(len(ids), 5)
        self.assertIn("OPS-01@2", ids)
        self.assertTrue(ids.isdisjoint({"OPS-01@1", "OPS-03@0"}))

    def test_stale_and_missing_evidence_block(self):
        stale = run_case(self.data, self.policies, "stale")
        missing = run_case(self.data, [p for p in self.policies if p["rule"] != "arrival"], "normal")
        self.assertEqual(stale["status"], "blocked")
        self.assertEqual(missing["status"], "blocked")
        self.assertNotIn("proposal", stale)
        self.assertNotIn("proposal", missing)


class APITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.server = make_server(0, Path(cls.folder.name) / "test.sqlite3")
        cls.worker = Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()
        cls.folder.cleanup()

    def request(self, path, body=None):
        req = Request(f"http://127.0.0.1:{self.server.server_port}{path}")
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=4) as response:
                return response.status, response.read()
        except HTTPError as error:
            with closing(error):
                return error.code, error.read()

    def plan(self, scenario):
        status, body = self.request("/api/plan", {"scenario": scenario, "deadline_hours": 48, "budget": 2400000})
        self.assertEqual(status, 201)
        return json.loads(body)["id"]

    def test_static_and_scenario_api(self):
        for path in ("/", "/ui.js", "/style.css", "/api/scenarios"):
            status, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)

    def test_approval_and_history(self):
        identifier = self.plan("normal")
        body = {"id": identifier, "decision": "approved", "note": "安排发运前复核班次"}
        self.assertEqual(self.request("/api/decision", body)[0], 200)
        self.assertEqual(self.request("/api/decision", body)[0], 409)
        history = json.loads(self.request("/api/history")[1])
        row = next(record for record in history if record["id"] == identifier)
        self.assertEqual((row["decision"], row["note"]), ("approved", body["note"]))

    def test_fulfillment_reconciles_partial_plan_and_short_receipt(self):
        identifier = self.plan("disruption")
        line = {"id": identifier, "lane_id": "SBY-MKS"}
        dispatch = {**line, "action": "dispatch"}
        receive = {**line, "action": "receive", "quantity": 11}
        api = "/api/fulfillment"
        self.assertEqual(self.request(api, dispatch)[0], 409)
        self.request("/api/decision", {"id": identifier, "decision": "approved", "note": "余量待补"})
        self.assertEqual(self.request(api, receive)[0], 409)
        self.assertEqual(self.request(api, {**dispatch, "lane_id": "BPN-MKS"})[0], 422)
        self.assertEqual(self.request(api, {**dispatch, "quantity": 13})[0], 422)
        with ThreadPoolExecutor(max_workers=2) as workers:
            statuses = list(workers.map(lambda _: self.request(api, dispatch)[0], range(2)))
        self.assertEqual(sorted(statuses), [200, 409])
        for quantity in (-1, 13, True):
            self.assertEqual(self.request(api, {**receive, "quantity": quantity})[0], 422)
        self.assertEqual(self.request(api, receive)[0], 422)
        ok = {**receive, "note": "一件破损，转人工补货"}
        status, payload = self.request(api, ok)
        self.assertEqual(status, 200)
        view = json.loads(payload)["execution"]
        self.assertEqual((view["complete"], view["received"], view["final_gap"]), (True, 11, 21))
        self.assertEqual(self.request(api, ok)[0], 409)
        rows = json.loads(self.request("/api/history")[1])
        self.assertEqual(next(r for r in rows if r["id"] == identifier)["execution"], view)

    def test_two_lanes_close_only_after_both_final_receipts(self):
        identifier = self.plan("normal")
        self.request("/api/decision", {"id": identifier, "decision": "approved"})
        for lane, quantity, gap in (("BPN-MKS", 20, None), ("SBY-MKS", 12, 0)):
            base = {"id": identifier, "lane_id": lane}
            self.assertEqual(self.request("/api/fulfillment", {**base, "action": "dispatch"})[0], 200)
            status, payload = self.request("/api/fulfillment", {**base, "action": "receive", "quantity": quantity})
            self.assertEqual(status, 200)
            view = json.loads(payload)["execution"]
            self.assertEqual(view["final_gap"], gap)
            self.assertEqual(view["complete"], gap is not None)

    def test_partial_requires_note_and_stale_cannot_be_approved(self):
        identifier = self.plan("disruption")
        body = {"id": identifier, "decision": "approved", "note": " "}
        self.assertEqual(self.request("/api/decision", body)[0], 422)
        self.assertEqual(self.request("/api/decision", {**body, "note": "剩余缺口人工安排"})[0], 200)
        stale = self.plan("stale")
        self.assertEqual(self.request("/api/decision", {**body, "id": stale})[0], 422)
        self.assertEqual(self.request("/api/decision", {**body, "id": stale, "decision": "rejected"})[0], 200)
