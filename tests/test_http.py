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

    def test_group_limit_and_usage_over_http(self):
        c = self.client
        self.assertEqual(
            c.put("/groups/G/limit", json={"max_exposure": 100.0}).status_code, 200)
        self.assertEqual(c.put("/groups/G/members/A").status_code, 200)
        self.assertEqual(c.put("/groups/G/members/B").status_code, 200)
        r1 = c.post("/events", json={"event_id": "e1", "account_id": "A",
                                     "seq": 1, "delta": 60.0})
        r2 = c.post("/events", json={"event_id": "e2", "account_id": "B",
                                     "seq": 1, "delta": 50.0})  # 组级超限
        print(f"[http] e1={r1.status_code} {r1.json()}")
        print(f"[http] e2={r2.status_code} {r2.json()}")
        self.assertEqual((r1.status_code, r1.json()["decision"]), (200, "ACCEPTED"))
        self.assertEqual((r2.status_code, r2.json()["reason"]),
                         (409, "GROUP_LIMIT_EXCEEDED"))
        usage = c.get("/groups/G/usage").json()
        print(f"[http] usage={usage}")
        self.assertEqual(usage["used"], 60.0)
        self.assertEqual(usage["remaining"], 40.0)
        self.assertEqual(usage["members"], [{"account_id": "A", "exposure": 60.0},
                                            {"account_id": "B", "exposure": 0.0}])

    def test_correction_over_http(self):
        c = self.client
        c.post("/events", json={"event_id": "e1", "account_id": "A",
                                "seq": 1, "delta": 100.0})
        ok = c.post("/corrections", json={"correction_id": "c1",
                                          "target_event_id": "e1",
                                          "new_delta": 40.0})
        dup = c.post("/corrections", json={"correction_id": "c1",
                                           "target_event_id": "e1",
                                           "new_delta": 40.0})
        unknown = c.post("/corrections", json={"correction_id": "c2",
                                               "target_event_id": "ghost",
                                               "cancel": True})
        invalid = c.post("/corrections", json={"correction_id": "c3",
                                               "target_event_id": "e1"})
        for r in (ok, dup, unknown, invalid):
            print(f"[http] {r.status_code} {r.json()}")
        self.assertEqual((ok.status_code, ok.json()["decision"]), (200, "ACCEPTED"))
        self.assertEqual((dup.status_code, dup.json()["decision"]), (200, "DUPLICATE"))
        self.assertEqual((unknown.status_code, unknown.json()["reason"]),
                         (409, "UNKNOWN_TARGET_EVENT"))
        self.assertEqual((invalid.status_code, invalid.json()["reason"]),
                         (409, "INVALID_CORRECTION"))
        self.assertEqual(c.get("/exposure/A").json()["exposure"], 40.0)

    def test_group_level_snapshot_over_http(self):
        c = self.client
        c.put("/groups/G/limit", json={"max_exposure": 1000.0})
        c.put("/groups/G/members/A")
        c.post("/events", json={"event_id": "e1", "account_id": "A",
                                "seq": 1, "delta": 100.0})
        snap = c.post("/stress/snapshot", json={"name": "s", "shocks": {"A": 0.5}})
        print(f"[http] snapshot={snap.json()}")
        self.assertEqual(snap.status_code, 200)
        self.assertEqual(snap.json()["group_base_exposure"], {"G": 100.0})
        self.assertEqual(snap.json()["group_stressed_exposure"], {"G": 150.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
