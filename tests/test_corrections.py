"""事件更正（冲正）：任意到达顺序最终口径一致、重复更正不重复生效、
非法/未知/冲突更正被可区分地拒绝，超限更正被拒且状态不留痕。"""
import unittest

from app.models import (
    CorrectionEvent,
    Decision,
    ExposureEvent,
    GroupLimitRule,
    LimitRule,
    RejectReason,
)
from app.service import RiskService

# 参考口径：e1 改为 60、e2 整笔撤销 -> 账户 A 最终敞口 60
EVENTS = [ExposureEvent("e1", "A", 1, 100.0), ExposureEvent("e2", "A", 2, 50.0)]
CORRECTIONS = [
    CorrectionEvent("c1", "e1", new_delta=60.0),
    CorrectionEvent("c2", "e2", cancel=True),
]
EXPECTED_FINAL = 60.0


def run_arrival_order(order):
    """按给定顺序投递事件/更正；被拒绝的更正收敛后重放，返回服务与首轮记录。"""
    svc = RiskService()
    svc.set_limit(LimitRule("A", 10_000.0))
    svc.set_group_limit(GroupLimitRule("G", 10_000.0))
    svc.assign_account("A", "G")
    first_pass = []
    for item in order:
        if isinstance(item, CorrectionEvent):
            rec = svc.submit_correction(item)
        else:
            rec = svc.submit_event(item)
        first_pass.append((item, rec))
        kind = "corr" if isinstance(item, CorrectionEvent) else "event"
        ident = getattr(item, "correction_id", None) or getattr(item, "event_id", None)
        print(f"[arrive:{kind}] {ident} -> {rec.decision.value} "
              f"reason={rec.reason.value}")
    # 收敛重放：所有原始事件已到位，此前因未知目标被拒的更正现在必须生效
    for item in order:
        if isinstance(item, CorrectionEvent):
            rec = svc.submit_correction(item)
            print(f"[replay:corr] {item.correction_id} -> {rec.decision.value} "
                  f"reason={rec.reason.value}")
            assert rec.decision in (Decision.ACCEPTED, Decision.DUPLICATE), rec
    return svc, first_pass


class CorrectionConvergenceTest(unittest.TestCase):
    def test_final_state_consistent_across_arrival_orders(self):
        e1, e2 = EVENTS
        c1, c2 = CORRECTIONS
        orders = [
            [e1, e2, c1, c2],            # 更正后到
            [c1, e1, c2, e2],            # 更正先到（先被拒，收敛后重放生效）
            [e1, c1, e2, c2],            # 交错到达
            [c2, c1, e2, e1],            # 全部逆序
        ]
        finals = []
        for i, order in enumerate(orders):
            print(f"=== order {i}: {[getattr(x, 'correction_id', None) or x.event_id for x in order]} ===")
            svc, _ = run_arrival_order(order)
            finals.append((svc.exposure("A"), svc.group_exposure("G")))
            print(f"[final] account={finals[-1][0]} group={finals[-1][1]}")
        for account_final, group_final in finals:
            self.assertEqual(account_final, EXPECTED_FINAL)
            self.assertEqual(group_final, EXPECTED_FINAL)

    def test_duplicate_correction_not_reapplied(self):
        svc = RiskService()
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        corr = CorrectionEvent("c1", "e1", new_delta=30.0)
        r1 = svc.submit_correction(corr)
        r2 = svc.submit_correction(corr)
        r3 = svc.submit_correction(corr)
        for r in (r1, r2, r3):
            print(f"[submit] {r.decision.value} reason={r.reason.value}")
        self.assertIs(r1.decision, Decision.ACCEPTED)
        self.assertIs(r2.decision, Decision.DUPLICATE)
        self.assertIs(r3.decision, Decision.DUPLICATE)
        # 不重复生效，也不会被当成新的敞口变动事件
        self.assertEqual(svc.exposure("A"), 30.0)
        self.assertEqual(len(svc.events_of("A")), 1)


class CorrectionRejectionTest(unittest.TestCase):
    def setUp(self):
        self.svc = RiskService()
        self.svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))

    def test_unknown_target_rejected(self):
        rec = self.svc.submit_correction(CorrectionEvent("c9", "ghost", new_delta=1.0))
        print(f"[reject] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.UNKNOWN_TARGET_EVENT)
        self.assertEqual(self.svc.exposure("A"), 100.0)

    def test_invalid_fields_rejected(self):
        cases = [
            CorrectionEvent("c1", "e1", new_delta=10.0, cancel=True),  # 互斥冲突
            CorrectionEvent("c2", "e1"),                               # 两者皆缺
            CorrectionEvent("c3", "e1", new_delta=float("nan")),       # 非有限
            CorrectionEvent("", "e1", new_delta=10.0),                 # 空 id
        ]
        for corr in cases:
            rec = self.svc.submit_correction(corr)
            print(f"[reject] {corr!r} -> {rec.reason.value} {rec.detail}")
            self.assertIs(rec.decision, Decision.REJECTED)
            self.assertIs(rec.reason, RejectReason.INVALID_CORRECTION)
        self.assertEqual(self.svc.exposure("A"), 100.0)

    def test_conflicting_correction_id_rejected(self):
        r1 = self.svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=30.0))
        r2 = self.svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=40.0))
        print(f"[conflict] {r2.decision.value} {r2.reason.value} {r2.detail}")
        self.assertIs(r1.decision, Decision.ACCEPTED)
        self.assertIs(r2.decision, Decision.REJECTED)
        self.assertIs(r2.reason, RejectReason.INVALID_CORRECTION)
        self.assertEqual(self.svc.exposure("A"), 30.0)

    def test_second_correction_on_same_target_rejected(self):
        r1 = self.svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=30.0))
        r2 = self.svc.submit_correction(CorrectionEvent("c2", "e1", cancel=True))
        print(f"[second] {r2.decision.value} {r2.reason.value} {r2.detail}")
        self.assertIs(r1.decision, Decision.ACCEPTED)
        self.assertIs(r2.decision, Decision.REJECTED)
        self.assertIs(r2.reason, RejectReason.INVALID_CORRECTION)
        self.assertEqual(self.svc.exposure("A"), 30.0)

    def test_limit_breaching_correction_rolled_back(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 80.0))
        rec = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=120.0))
        print(f"[rollback] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertEqual(svc.exposure("A"), 80.0)  # 已回滚
        # 更正未生效，合法的后续更正仍可被接受
        ok = svc.submit_correction(CorrectionEvent("c2", "e1", new_delta=90.0))
        self.assertIs(ok.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 90.0)

    def test_group_limit_breaching_correction_rolled_back(self):
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 100.0))
        svc.assign_account("A", "G")
        svc.submit_event(ExposureEvent("e1", "A", 1, 80.0))
        rec = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=120.0))
        print(f"[group-rollback] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        self.assertEqual(svc.exposure("A"), 80.0)
        self.assertEqual(svc.group_exposure("G"), 80.0)

    def test_correction_updates_group_usage(self):
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 1000.0))
        svc.assign_account("A", "G")
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        svc.submit_event(ExposureEvent("e2", "A", 2, 50.0))
        svc.submit_correction(CorrectionEvent("c1", "e2", cancel=True))
        usage = svc.group_usage("G")
        print(f"[usage-after-correction] {usage}")
        self.assertEqual(usage["used"], 100.0)
        self.assertEqual(usage["members"], [{"account_id": "A", "exposure": 100.0}])
        self.assertEqual(usage["remaining"], 900.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
