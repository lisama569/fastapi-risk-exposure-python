"""幂等重放、乱序到达与负敞口防护测试。"""
import unittest
from decimal import Decimal

from app.models import EventType, RejectReason
from tests._helpers import evt, fresh_stack, log_case


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.service, _ = fresh_stack()
        self.store.ensure_account("acct-1", Decimal("1000"))

    def test_duplicate_delivery_applied_once(self) -> None:
        """同一事件重复投递：只扣减一次，结论稳定。"""
        e = evt("e-1", "acct-1", 1, EventType.INCREASE, "100", "CASH")
        first = self.service.report_event(e)
        second = self.service.report_event(e.model_copy())
        third = self.service.report_event(e.model_copy())
        log_case(
            "重复投递 e-1 三次",
            f"event={e.model_dump(mode='json')} x3",
            f"first.accepted={first.accepted}; second.duplicate={second.duplicate}; "
            f"敞口序列=({first.exposure_after},{second.exposure_after},{third.exposure_after})",
        )
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted and third.accepted)
        self.assertTrue(second.duplicate and third.duplicate)
        self.assertEqual(self.service.replay("acct-1"), Decimal("100"))
        self.assertEqual(self.store.get_version("acct-1"), 1)

    def test_out_of_order_converges(self) -> None:
        """乱序到达与按序去重结果一致。"""
        ordered = [
            evt("a", "acct-1", 1, EventType.INCREASE, "100", "CASH"),
            evt("b", "acct-1", 2, EventType.DECREASE, "40", "CASH"),
            evt("c", "acct-1", 3, EventType.INCREASE, "25", "CASH"),
        ]
        shuffled = [ordered[2], ordered[0], ordered[1]]  # seq 3,1,2 乱序
        acks = [self.service.report_event(e) for e in shuffled]
        exposure = self.service.replay("acct-1")
        log_case(
            "乱序到达 seq=3,1,2",
            [f"{e.event_id}(seq={e.seq},{e.type.value} {e.amount})" for e in shuffled],
            [f"{a.event_id}:accepted={a.accepted},after={a.exposure_after}" for a in acks]
            + [f"按序重放收敛敞口={exposure}"],
        )
        for a in acks:
            self.assertTrue(a.accepted)
        self.assertEqual(exposure, Decimal("85"))
        self.assertEqual(self.store.get_exposure("acct-1"), Decimal("85"))

    def test_decrease_without_baseline_rejected(self) -> None:
        """释放额超过在先增加：拒绝且原因可区分，不产生负数。"""
        good = evt("g", "acct-1", 1, EventType.INCREASE, "30", "CASH")
        bad = evt("d", "acct-1", 2, EventType.DECREASE, "50", "CASH")
        self.service.report_event(good)
        r1 = self.service.report_event(bad)
        r2 = self.service.report_event(bad.model_copy())  # 重放拒绝结论稳定
        log_case(
            "DECREASE 缺少在先 INCREASE 基准",
            f"INCREASE 30 @seq1; DECREASE 50 @seq2（重放两次）",
            f"reason={r1.reason}; accepted={r1.accepted}; 敞口={r1.exposure_after}; "
            f"重放 reason={r2.reason} duplicate={r2.duplicate}",
        )
        self.assertFalse(r1.accepted)
        self.assertEqual(r1.reason, RejectReason.NEGATIVE_EXPOSURE)
        self.assertEqual(r2.reason, RejectReason.NEGATIVE_EXPOSURE)
        self.assertTrue(r2.duplicate)
        self.assertEqual(self.service.replay("acct-1"), Decimal("30"))

    def test_late_decrease_becomes_valid_after_increase(self) -> None:
        """先到的大额释放因缺少在先增加而暂不生效；在先增加补齐后再次提交，
        结论随收敛状态翻转，最终敞口与按序去重一致（10），不出现负数。"""
        dec = evt("d", "acct-1", 2, EventType.DECREASE, "50", "CASH")
        inc = evt("i", "acct-1", 1, EventType.INCREASE, "60", "CASH")
        r_late = self.service.report_event(dec)  # seq2 先到，无基准
        self.assertEqual(r_late.reason, RejectReason.NEGATIVE_EXPOSURE)
        self.assertEqual(r_late.exposure_after, Decimal("0"))
        r_inc = self.service.report_event(inc)
        r_replay = self.service.report_event(dec.model_copy())
        log_case(
            "乱序：释放先于增加到达",
            "DECREASE 50 @seq2 先到；随后 INCREASE 60 @seq1；再次提交 DECREASE",
            f"初判={r_late.reason}(after={r_late.exposure_after}); "
            f"增加入账={r_inc.accepted}; 重提 accepted={r_replay.accepted} "
            f"duplicate={r_replay.duplicate} after={r_replay.exposure_after}",
        )
        self.assertTrue(r_inc.accepted)
        self.assertTrue(r_replay.accepted)
        self.assertTrue(r_replay.duplicate)
        self.assertEqual(self.service.replay("acct-1"), Decimal("10"))
        self.assertEqual(self.store.get_exposure("acct-1"), Decimal("10"))

    def test_conflicting_duplicate_rejected(self) -> None:
        """相同 event_id 载荷冲突：明确拒绝。"""
        e1 = evt("x", "acct-1", 1, EventType.INCREASE, "10", "CASH")
        e2 = evt("x", "acct-1", 1, EventType.INCREASE, "99", "CASH")
        self.service.report_event(e1)
        r = self.service.report_event(e2)
        log_case(
            "重复 ID 载荷冲突",
            f"首次 amount=10；冲突提交 amount=99",
            f"reason={r.reason}; 敞口={r.exposure_after}",
        )
        self.assertEqual(r.reason, RejectReason.DUPLICATE_CONFLICT)
        self.assertEqual(self.service.replay("acct-1"), Decimal("10"))

    def test_invalid_amount_rejected(self) -> None:
        """负数/非有限金额在领域层被拒绝。"""
        e = evt("n", "acct-1", 1, EventType.INCREASE, "-5", "CASH")
        r = self.service.report_event(e)
        self.assertEqual(r.reason, RejectReason.INVALID_AMOUNT)
        log_case("负数金额", "INCREASE -5", f"reason={r.reason}")


if __name__ == "__main__":
    unittest.main()
