"""HTTP 入口：与服务层语义一致（幂等、限额、情景拒绝原因）。"""
import importlib
import unittest

from fastapi.testclient import TestClient


class HttpEntryTest(unittest.TestCase):
    def setUp(self):
        # 每个用例重置全局服务状态
        import main
        importlib.reload(main)
        self.client = TestClient(main.app)

    def test_event_limit_and_replay_over_http(self):
        c = self.client
        self.assertEqual(c.put("/limits/A", json={"max_exposure": 100.0}).status_code, 200)
        body = {"event_id": "e1", "account_id": "A", "seq": 1, "delta": 80.0}
        r1 = c.post("/events", json=body)
        r2 = c.post("/events", json=body)  # 重放
        r3 = c.post("/events", json={"event_id": "e2", "account_id": "A",
                                     "seq": 2, "delta": 50.0})
        for r in (r1, r2, r3):
            print(f"[http] {r.status_code} {r.json()}")
        self.assertEqual((r1.status_code, r1.json()["decision"]), (200, "ACCEPTED"))
        self.assertEqual((r2.status_code, r2.json()["decision"]), (200, "DUPLICATE"))
        self.assertEqual((r3.status_code, r3.json()["reason"]), (409, "LIMIT_EXCEEDED"))
        self.assertEqual(c.get("/exposure/A").json()["exposure"], 80.0)

    def test_stress_snapshot_over_http(self):
        c = self.client
        c.post("/events", json={"event_id": "e1", "account_id": "A",
                                "seq": 1, "delta": 100.0})
        ok = c.post("/stress/snapshot", json={"name": "s", "shocks": {"A": 0.5}})
        missing = c.post("/stress/snapshot", json={"name": "s", "shocks": {"ZZZ": 0.5}})
        invalid = c.post("/stress/snapshot", json={"name": "s", "shocks": {"A": -2.0}})
        print(f"[http] ok={ok.status_code} {ok.json()}")
        print(f"[http] missing={missing.status_code} {missing.json()}")
        print(f"[http] invalid={invalid.status_code} {invalid.json()}")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["stressed_exposure"]["A"], 150.0)
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(missing.json()["detail"]["reason"], "MISSING_BASELINE")
        self.assertEqual(invalid.json()["detail"]["reason"], "INVALID_SCENARIO")


if __name__ == "__main__":
    unittest.main(verbosity=2)
