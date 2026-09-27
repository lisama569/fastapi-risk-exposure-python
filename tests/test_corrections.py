"""事件更正（冲正）：顺序无关的收敛一致性、幂等与可区分拒绝原因。"""
import unittest

from app.models import (
    Decision,
    EventCorrection,
    ExposureEvent,
    GroupMembership,
    GroupRule,
    LimitLevel,
    LimitRule,
    RejectReason,
)
from app.service import RiskService


def deliver(svc, item):
    """统一投递：ExposureEvent 走上报，EventCorrection 走更正。"""
    if isinstance(item, ExposureEvent):
        return svc.submit_event(item)
    return svc.submit_correction(item)


def item_key(item):
    return item.correction_id if isinstance(item, EventCorrection) else item.event_id


def converge(svc, items):
    """重复投递直到没有新结论变化：ORIGINAL_NOT_FOUND 在原始上报到达后重试生效。

    返回 {id: (decision, reason)} 的收敛结论。乱序语义下，调用方可以用任意
    排列调用本函数，收敛结果必须一致。
    """
    pending = list(items)
    results: dict[str, tuple] = {}
    while True:
        progressed = False
        remaining = []
        for item in pending:
            rec = deliver(svc, item)
            key = item_key(item)
            if rec.reason is RejectReason.ORIGINAL_NOT_FOUND:
                remaining.append(item)  # 原始上报尚未到达，稍后重试
                continue
            progressed = True
            results[key] = (rec.decision, rec.reason)
        pending = remaining
        if not pending:
            return results
        if not progressed:  # 原始上报始终没来：记录最终拒绝
            for item in pending:
                rec = deliver(svc, item)
                key = item_key(item)
                results[key] = (rec.decision, rec.reason)
            return results


class CorrectionConvergenceTest(unittest.TestCase):
    def setUp(self):
        self.events = [
            ExposureEvent("e1", "A", 1, 100.0),
            ExposureEvent("e2", "A", 2, -30.0),
            ExposureEvent("e3", "A", 3, 50.0),
        ]
        # 把 e1 的 100 更正为 40；撤销 e3
        self.corr_e1 = EventCorrection("c1", "e1", new_delta=40.0)
        self.cancel_e3 = EventCorrection("c2", "e3", cancel=True)
        # 最终生效序列：e1=40, e2=-30 -> 10；e3 撤销 -> 10
        self.final_exposure = 10.0

    def test_arrival_orders_converge_identically(self):
        import itertools

        bundles = [
            [self.events[0], self.corr_e1],
            [self.events[2], self.cancel_e3],
            [self.events[1]],
        ]
        orderings = list(itertools.permutations(range(3)))
        finals = set()
        for oi, order in enumerate(orderings):
            svc = RiskService()
            svc.set_limit(LimitRule("A", 10_000.0))
            flat = [x for i in order for x in bundles[i]]
            results = converge(svc, flat)
            finals.add(svc.exposure("A"))
            # 每笔事件/更正的收敛结论在不同顺序下一致
            for key, val in results.items():
                print(f"[order {oi}] {key} -> {val[0].value}/{val[1].value}")
            self.assertEqual(
                results["c1"], (Decision.ACCEPTED, RejectReason.NONE)
            )
            self.assertEqual(
                results["c2"], (Decision.ACCEPTED, RejectReason.NONE)
            )
        self.assertEqual(finals, {self.final_exposure})
        print(f"[converge] {len(orderings)} 种到达顺序最终敞口均为 "
              f"{self.final_exposure}")

    def test_duplicate_correction_applies_once(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 10_000.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        c = EventCorrection("c1", "e1", new_delta=30.0)
        r1 = svc.submit_correction(c)
        r2 = svc.submit_correction(c)
        r3 = svc.submit_correction(EventCorrection("c1", "e1", new_delta=30.0))
        print(f"[correction replays] {r1.decision.value} {r2.decision.value} "
              f"{r3.decision.value}")
        self.assertIs(r1.decision, Decision.ACCEPTED)
        self.assertIs(r2.decision, Decision.DUPLICATE)
        self.assertIs(r3.decision, Decision.DUPLICATE)
        self.assertEqual(svc.exposure("A"), 30.0)
        # 更正没有被当成新事件
        self.assertEqual([e.event_id for e in svc.events_of("A")], ["e1"])

    def test_correction_reject_reasons_distinguishable(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))

        cases = [
            ("引用不存在", EventCorrection("c0", "ghost", new_delta=1.0),
             RejectReason.ORIGINAL_NOT_FOUND),
            ("金额非有限", EventCorrection("c1", "e1", new_delta=float("nan")),
             RejectReason.INVALID_CORRECTION),
            ("金额与撤销并存",
             EventCorrection("c2", "e1", new_delta=1.0, cancel=True),
             RejectReason.INVALID_CORRECTION),
            ("空标识", EventCorrection("", "e1", new_delta=1.0),
             RejectReason.INVALID_CORRECTION),
        ]
        for label, corr, expected in cases:
            rec = svc.submit_correction(corr)
            print(f"[reject:{label}] {rec.reason.value}")
            self.assertIs(rec.reason, expected)
        self.assertEqual(svc.exposure("A"), 100.0)

    def test_second_correction_on_same_original_conflicts(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 10_000.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        self.assertIs(svc.submit_correction(
            EventCorrection("c1", "e1", new_delta=40.0)).decision,
            Decision.ACCEPTED)
        rec = svc.submit_correction(EventCorrection("c2", "e1", cancel=True))
        self.assertIs(rec.reason, RejectReason.CORRECTION_CONFLICT)
        self.assertEqual(svc.exposure("A"), 40.0)
        print(f"[conflict] {rec.detail}")

    def test_correction_revalidates_both_limit_levels(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.set_limit(LimitRule("B", 100.0))
        svc.set_group_limit(GroupRule("G", 100.0))
        svc.assign_account(GroupMembership("A", "G"))
        svc.assign_account(GroupMembership("B", "G"))
        svc.submit_event(ExposureEvent("e1", "A", 1, 80.0))
        svc.submit_event(ExposureEvent("e2", "B", 1, 20.0))  # 组合计 100

        # 把 B 的 20 改成 40：账户层 OK（40<=100），组层 120 > 100
        r_group = svc.submit_correction(
            EventCorrection("c1", "e2", new_delta=40.0))
        self.assertIs(r_group.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        self.assertIs(r_group.level, LimitLevel.GROUP)
        self.assertEqual(svc.exposure("B"), 20.0)  # 未生效
        self.assertEqual(svc.group_exposure("G"), 100.0)

        # 撤销 A 的 80 后再更 B：组层 40 <= 100，可生效
        self.assertIs(svc.submit_correction(
            EventCorrection("c2", "e1", cancel=True)).decision,
            Decision.ACCEPTED)
        r_ok = svc.submit_correction(
            EventCorrection("c1", "e2", new_delta=40.0))
        self.assertIs(r_ok.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 0.0)
        self.assertEqual(svc.exposure("B"), 40.0)
        self.assertEqual(svc.group_exposure("G"), 40.0)
        print("[revalidate] 账户层/组层在更正后重新成立")

    def test_correction_keeps_account_prefix_within_limit(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 60.0))
        svc.submit_event(ExposureEvent("e2", "A", 2, 40.0))  # 恰好 100
        # 把 e1 从 60 改成 90：前缀 90 OK，但 e2 之后 130 > 100
        rec = svc.submit_correction(
            EventCorrection("c1", "e1", new_delta=90.0))
        self.assertIs(rec.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertIs(rec.level, LimitLevel.ACCOUNT)
        self.assertEqual(svc.exposure("A"), 100.0)
        print(f"[prefix] {rec.detail}")

    def test_rejected_original_is_not_correctable(self):
        """被限额拒绝、从未生效的事件不能作为更正对象（直到它真正生效）。"""
        svc = RiskService()
        svc.set_limit(LimitRule("A", 10.0))
        rejected = svc.submit_event(ExposureEvent("e1", "A", 1, 50.0))
        self.assertIs(rejected.reason, RejectReason.LIMIT_EXCEEDED)
        rec = svc.submit_correction(EventCorrection("c1", "e1", new_delta=5.0))
        self.assertIs(rec.reason, RejectReason.ORIGINAL_NOT_FOUND)
        print(f"[rejected-original] {rec.detail}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
