"""压力情景：可复现快照，非法参数与缺失基准被明确拒绝。"""
import unittest

from app.models import ExposureEvent, StressScenario
from app.service import RiskService


def build_service() -> RiskService:
    svc = RiskService()
    svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
    svc.submit_event(ExposureEvent("e2", "A", 2, -20.0))
    svc.submit_event(ExposureEvent("e3", "B", 1, 50.0))
    return svc


class StressTest(unittest.TestCase):
    def test_snapshot_is_reproducible(self):
        svc = build_service()
        scenario = StressScenario("rates-up", {"A": 0.5, "B": -0.2})
        snap1 = svc.stress_snapshot(scenario)
        snap2 = svc.stress_snapshot(StressScenario("rates-up", {"A": 0.5, "B": -0.2}))
        print(f"[scenario] {scenario}")
        print(f"[snapshot1] {snap1}")
        print(f"[snapshot2] {snap2}")
        self.assertEqual(snap1, snap2)
        self.assertEqual(snap1["base_exposure"], {"A": 80.0, "B": 50.0})
        self.assertEqual(snap1["stressed_exposure"], {"A": 120.0, "B": 40.0})
        self.assertEqual(len(snap1["hash"]), 64)

    def test_invalid_scenarios_rejected(self):
        svc = build_service()
        cases = [
            ("空名称", StressScenario("", {"A": 0.1}), "INVALID_SCENARIO"),
            ("空冲击", StressScenario("s", {}), "INVALID_SCENARIO"),
            ("冲击低于-100%", StressScenario("s", {"A": -1.5}), "INVALID_SCENARIO"),
            ("非有限冲击", StressScenario("s", {"A": float("inf")}), "INVALID_SCENARIO"),
            ("缺失基准", StressScenario("s", {"GHOST": 0.1}), "MISSING_BASELINE"),
        ]
        for label, scenario, expected in cases:
            with self.assertRaises(ValueError) as ctx:
                svc.stress_snapshot(scenario)
            print(f"[reject:{label}] {ctx.exception}")
            self.assertTrue(str(ctx.exception).startswith(expected),
                            f"{label}: 期望 {expected}，实际 {ctx.exception}")

    def test_snapshot_does_not_mutate_base(self):
        svc = build_service()
        before = svc.exposure("A")
        svc.stress_snapshot(StressScenario("s", {"A": 2.0}))
        self.assertEqual(svc.exposure("A"), before)
        print(f"[no-mutation] 情景重算前后基准敞口均为 {before}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
