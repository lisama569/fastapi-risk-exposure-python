"""组合级压力情景：账户层+账户组层结果可复现，且与更正序列、额度占用明细一致。"""
import unittest

from app.models import (
    CorrectionEvent,
    ExposureEvent,
    GroupLimitRule,
    StressScenario,
)
from app.service import RiskService

EVENTS = [
    ExposureEvent("e1", "A", 1, 100.0),
    ExposureEvent("e2", "A", 2, -20.0),
    ExposureEvent("e3", "B", 1, 50.0),
    ExposureEvent("e4", "C", 1, 30.0),
]
CORRECTIONS = [CorrectionEvent("c1", "e3", new_delta=70.0)]
SCENARIO = StressScenario("rates-up", {"A": 0.5, "B": -0.2, "C": 0.1})


def build_service(order):
    svc = RiskService()
    svc.set_group_limit(GroupLimitRule("G", 10_000.0))
    svc.assign_account("A", "G")
    svc.assign_account("B", "G")
    for item in order:
        if isinstance(item, CorrectionEvent):
            svc.submit_correction(item)
        else:
            svc.submit_event(item)
    # 收敛重放：让先到而被拒的更正生效
    for item in order:
        if isinstance(item, CorrectionEvent):
            svc.submit_correction(item)
    return svc


class GroupStressTest(unittest.TestCase):
    def test_group_level_snapshot_is_reproducible(self):
        svc = build_service(EVENTS + CORRECTIONS)
        snap1 = svc.stress_snapshot(SCENARIO)
        snap2 = svc.stress_snapshot(StressScenario("rates-up",
                                                   {"A": 0.5, "B": -0.2, "C": 0.1}))
        print(f"[snapshot1] {snap1}")
        print(f"[snapshot2] {snap2}")
        self.assertEqual(snap1, snap2)
        # 账户层：A=80, B=70(e3 被更正为 70), C=30
        self.assertEqual(snap1["base_exposure"], {"A": 80.0, "B": 70.0, "C": 30.0})
        self.assertEqual(snap1["stressed_exposure"],
                         {"A": 120.0, "B": 56.0, "C": 33.0})
        # 组层：G = A + B（C 不属于任何组）
        self.assertEqual(snap1["group_base_exposure"], {"G": 150.0})
        self.assertEqual(snap1["group_stressed_exposure"], {"G": 176.0})
        self.assertEqual(len(snap1["hash"]), 64)

    def test_same_events_and_corrections_any_order_same_snapshot(self):
        e1, e2, e3, e4 = EVENTS
        (c1,) = CORRECTIONS
        orders = [
            [e1, e2, e3, e4, c1],
            [c1, e3, e1, e4, e2],   # 更正先到
            [e3, c1, e2, e1, e4],   # 交错
        ]
        snapshots = []
        for i, order in enumerate(orders):
            svc = build_service(order)
            snap = svc.stress_snapshot(SCENARIO)
            snapshots.append(snap)
            print(f"[order {i}] hash={snap['hash']}")
        for snap in snapshots[1:]:
            self.assertEqual(snap, snapshots[0])
            self.assertEqual(snap["hash"], snapshots[0]["hash"])

    def test_snapshot_consistent_with_group_usage(self):
        svc = build_service(EVENTS + CORRECTIONS)
        snap = svc.stress_snapshot(SCENARIO)
        usage = svc.group_usage("G")
        print(f"[usage] {usage}")
        print(f"[snapshot.group_base] {snap['group_base_exposure']}")
        # 组层基准 == 占用明细总占用 == 成员账户基准之和
        self.assertEqual(snap["group_base_exposure"]["G"], usage["used"])
        member_sum = sum(m["exposure"] for m in usage["members"])
        self.assertEqual(usage["used"], member_sum)
        for m in usage["members"]:
            self.assertEqual(m["exposure"], snap["base_exposure"][m["account_id"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
