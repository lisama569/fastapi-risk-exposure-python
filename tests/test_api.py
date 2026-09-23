"""HTTP 入口与领域服务结论一致性、错误原因可区分性测试。"""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from fastapi.testclient import TestClient

from app.server import app, risk_service, scenario_engine, store
from tests._helpers import log_case


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        # 重置模块级内存状态，保证用例隔离
        store.__init__()
        risk_service.__init__(store)
        scenario_engine.__init__(store)

    def test_event_idempotent_and_reason_in_body(self) -> None:
        self.client.post("/accounts/acct-1", json={"limit": "1000"})
        payload = {
            "event_id": "E1", "account_id": "acct-1", "seq": 1,
            "type": "INCREASE", "amount": "120.50", "asset_class": "CASH",
        }
        r1 = self.client.post("/events", json=payload)
        r2 = self.client.post("/events", json=payload)
        log_case("HTTP 幂等事件", payload,
                 f"status={r1.status_code}/{r2.status_code}; "
                 f"duplicate={r2.json()['duplicate']}; after={r2.json()['exposure_after']}")
        self.assertEqual(r1.status_code, 200)
        self.assertTrue(r1.json()["accepted"])
        self.assertTrue(r2.json()["duplicate"])
        self.assertEqual(r2.json()["exposure_after"], "120.50")

        bad = {**payload, "event_id": "E2", "seq": 2,
               "type": "DECREASE", "amount": "999"}
        r3 = self.client.post("/events", json=bad)
        self.assertFalse(r3.json()["accepted"])
        self.assertEqual(r3.json()["reason"], "NEGATIVE_EXPOSURE")

    def test_concurrent_limit_http(self) -> None:
        self.client.post("/accounts/acct-c", json={"limit": "500"})

        def hit(i: int):
            return self.client.post("/limits/check", json={
                "request_id": f"h{i}", "account_id": "acct-c", "amount": "150",
            }).json()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(hit, range(6)))
        approved = [r for r in results if r["approved"]]
        log_case("HTTP 6 并发 x150 / 限额500",
                 "6 requests amount=150",
                 f"approved={len(approved)} rejected={6 - len(approved)} "
                 f"reasons={sorted({r['reason'] for r in results if not r['approved']})}")
        self.assertEqual(len(approved), 3)
        self.assertEqual(store.get_exposure("acct-c"), Decimal("450"))

    def test_scenario_validation_and_missing_baseline(self) -> None:
        # 非法情景参数 -> 422 INVALID_SCENARIO
        r = self.client.post("/scenarios", json={
            "scenario_id": "bad", "haircuts": {"CASH": 1.5}})
        log_case("HTTP 非法折扣率", {"haircuts": {"CASH": 1.5}},
                 f"status={r.status_code} body={r.json()}")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["reason"], "INVALID_SCENARIO")

        # 合法情景
        r = self.client.post("/scenarios", json={
            "scenario_id": "crash",
            "haircuts": {"CASH": "0.5"},
            "scenario_limit": "10",
        })
        self.assertEqual(r.status_code, 201)

        # 缺失基准账户 -> 422 MISSING_BASELINE
        r = self.client.get("/scenarios/crash/accounts/ghost")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["reason"], "MISSING_BASELINE")

        # 未知情景 -> 422 SCENARIO_NOT_FOUND
        self.client.post("/accounts/acct-1", json={"limit": None})
        r = self.client.get("/scenarios/unknown/accounts/acct-1")
        self.assertEqual(r.json()["detail"]["reason"], "SCENARIO_NOT_FOUND")

    def test_scenario_happy_path_http(self) -> None:
        self.client.post("/accounts/acct-1", json={"limit": None})
        self.client.post("/events", json={
            "event_id": "E1", "account_id": "acct-1", "seq": 1,
            "type": "INCREASE", "amount": "100", "asset_class": "CASH"})
        self.client.post("/scenarios", json={
            "scenario_id": "half", "haircuts": {"CASH": "0.5"}})
        r = self.client.get("/scenarios/half/accounts/acct-1")
        body = r.json()
        log_case("HTTP 情景重算", "CASH 100, haircut 0.5", body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["stressed_exposure"], "50.0")
        self.assertIsNone(body["reason"])


if __name__ == "__main__":
    unittest.main()
