"""组合级情景重算与额度占用明细：可复现、与逐笔判定一致。"""
import unittest

from app.models import (
    Decision,
    EventCorrection,
    ExposureEvent,
    GroupMembership,
    GroupRule,
    LimitRule,
    StressScenario,
)
from app.service import RiskService


def build_service() -> RiskService:
    svc = RiskService()
    svc.set_limit(LimitRule("A", 10_000.0))
    svc.set_limit(LimitRule("B", 10_000.0))
    svc.set_group_limit(GroupRule("G", 10_000.0))
    svc.assign_account(GroupMembership("A", "G"))
    svc.assign_account(GroupMembership("B", "G"))
    svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
    svc.submit_event(ExposureEvent("e2", "A", 2, -20.0))
    svc.submit_event(ExposureEvent("e3", "B", 1, 50.0))
    return svc


class GroupStressTest(unittest.TestCase):
    def test_group_snapshot_reproducible(self):
        svc = build_service()
        scenario = StressScenario(
            "mix", {"A": 0.5}, {"G": -0.25}
        )
        snap1 = svc.stress_snapshot(scenario)
        snap2 = svc.stress_snapshot(
            StressScenario("mix", {"A": 0.5}, {"G": -0.25}))
        print(f"[snapshot1] {snap1}")
        print(f"[snapshot2] {snap2}")
        self.assertEqual(snap1, snap2)
        # 账户层：A=80*1.5=120，B=50 无冲击
        self.assertEqual(snap1["stressed_exposure"], {"A": 120.0, "B": 50.0})
        # 组合层：基准 80+50=130，冲击 -25% -> 97.5
        self.assertEqual(snap1["group_base_exposure"], {"G": 130.0})
        self.assertEqual(snap1["group_stressed_exposure"], {"G": 97.5})
        self.assertEqual(len(snap1["hash"]), 64)

    def test_snapshot_stable_under_correction_arrival_order(self):
        """相同事件与更正集合，不同到达顺序 -> 相同快照哈希。"""
        import itertools

        events = [
            ExposureEvent("e1", "A", 1, 100.0),
            ExposureEvent("e2", "A", 2, -20.0),
            ExposureEvent("e3", "B", 1, 50.0),
        ]
        corr = EventCorrection("c1", "e1", new_delta=60.0)
        hashes = set()
        for order in itertools.permutations(range(4)):
            svc = RiskService()
            svc.set_group_limit(GroupRule("G", 10_000.0))
            svc.assign_account(GroupMembership("A", "G"))
            svc.assign_account(GroupMembership("B", "G"))
            items = [events[0], events[1], events[2], corr]
            # 先全部投递一遍（更正可能因 ORIGINAL_NOT_FOUND 被拒），再补投被拒更正
            for i in order:
                it = items[i]
                if isinstance(it, ExposureEvent):
                    svc.submit_event(it)
                else:
                    svc.submit_correction(it)
            svc.submit_correction(corr)  # 收敛补投（重复提交幂等）
            snap = svc.stress_snapshot(StressScenario("s", {}, {"G": 0.1}))
            hashes.add(snap["hash"])
        self.assertEqual(len(hashes), 1)
        print(f"[reproducible] 4! 种顺序快照哈希唯一: {hashes.pop()[:16]}...")

    def test_occupancy_matches_decisions_and_corrections(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.set_limit(LimitRule("B", 100.0))
        svc.set_group_limit(GroupRule("G", 150.0))
        svc.assign_account(GroupMembership("A", "G"))
        svc.assign_account(GroupMembership("B", "G"))

        r1 = svc.submit_event(ExposureEvent("e1", "A", 1, 90.0))
        r2 = svc.submit_event(ExposureEvent("e2", "B", 1, 70.0))  # 组层拒绝
        r3 = svc.submit_event(ExposureEvent("e3", "B", 1, 40.0))  # 通过
        rc = svc.submit_correction(EventCorrection("c1", "e1", new_delta=60.0))
        for label, rec in [("e1", r1), ("e2", r2), ("e3", r3), ("c1", rc)]:
            print(f"[{label}] {rec.decision.value}/{rec.reason.value}")
        self.assertIs(r1.decision, Decision.ACCEPTED)
        self.assertIs(r2.decision, Decision.REJECTED)
        self.assertIs(r3.decision, Decision.ACCEPTED)
        self.assertIs(rc.decision, Decision.ACCEPTED)

        occ = svc.group_occupancy("G")
        print(f"[occupancy] {occ}")
        # A 生效 60（更正后），B 生效 40（被拒的 70 不计）
        self.assertEqual(occ["per_account"], {"A": 60.0, "B": 40.0})
        self.assertEqual(occ["used"], 100.0)
        self.assertEqual(occ["remaining"], 50.0)
        self.assertEqual(occ["max_exposure"], 150.0)
        # 占用明细与逐笔判定对得上：占用 = 各账户已接受事件（经更正）之和
        self.assertEqual(occ["used"], svc.exposure("A") + svc.exposure("B"))

    def test_invalid_group_scenarios_rejected(self):
        svc = build_service()
        cases = [
            ("组冲击低于-100%",
             StressScenario("s", {}, {"G": -1.5}), "INVALID_SCENARIO"),
            ("组冲击非有限",
             StressScenario("s", {}, {"G": float("inf")}), "INVALID_SCENARIO"),
            ("缺失组基准",
             StressScenario("s", {}, {"GHOST": 0.1}), "MISSING_BASELINE"),
        ]
        for label, scenario, expected in cases:
            with self.assertRaises(ValueError) as ctx:
                svc.stress_snapshot(scenario)
            print(f"[reject:{label}] {ctx.exception}")
            self.assertTrue(str(ctx.exception).startswith(expected))

    def test_account_only_snapshot_backward_compatible(self):
        """不带组合冲击时快照字段与旧版一致。"""
        svc = build_service()
        snap = svc.stress_snapshot(StressScenario("s", {"A": 0.5}))
        self.assertEqual(
            set(snap.keys()),
            {"scenario", "shocks", "base_exposure", "stressed_exposure", "hash"},
        )
        self.assertEqual(snap["stressed_exposure"]["A"], 120.0)
        print(f"[compat] {sorted(snap.keys())}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
