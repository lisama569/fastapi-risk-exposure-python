"""账户组共享额度：并发下两层额度不被突破，拒绝层级可区分。"""
import threading
import unittest
from collections import Counter

from app.models import (
    Decision,
    ExposureEvent,
    GroupMembership,
    GroupRule,
    LimitLevel,
    LimitRule,
    RejectReason,
)
from app.service import RiskService


class GroupLimitConcurrencyTest(unittest.TestCase):
    def test_concurrent_group_capacity_never_breached(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 10_000.0))
        svc.set_limit(LimitRule("B", 10_000.0))
        # 账户层都很宽，真正卡人的是组层 100
        svc.set_group_limit(GroupRule("G", 100.0))
        svc.assign_account(GroupMembership("A", "G"))
        svc.assign_account(GroupMembership("B", "G"))

        records: list[tuple[ExposureEvent, object]] = []
        rec_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def worker(i):
            account = "A" if i % 2 == 0 else "B"
            ev = ExposureEvent(f"g{i}", account, i + 1, 20.0)
            barrier.wait()  # 尽量同时发起，制造真实竞争
            rec = svc.submit_event(ev)
            with rec_lock:
                records.append((ev, rec))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        counts = Counter(r.decision for _, r in records)
        print(f"[summary] {dict(counts)}")
        for ev, rec in sorted(records, key=lambda x: x[0].seq):
            print(f"[input] {ev.event_id} account={ev.account_id} delta=20 -> "
                  f"{rec.decision.value}/{rec.reason.value}/{rec.level.value}")

        # 10 笔 * 20，组上限 100：恰好 5 笔通过、5 笔拒绝
        self.assertEqual(counts[Decision.ACCEPTED], 5)
        self.assertEqual(counts[Decision.REJECTED], 5)
        self.assertTrue(all(
            r.reason is RejectReason.GROUP_LIMIT_EXCEEDED
            and r.level is LimitLevel.GROUP
            for _, r in records if r.decision is Decision.REJECTED
        ))
        self.assertLessEqual(svc.group_exposure("G"), 100.0 + 1e-9)
        self.assertAlmostEqual(svc.group_exposure("G"), 100.0)

        # 占用明细与逐笔结论对得上
        occ = svc.group_occupancy("G")
        print(f"[occupancy] {occ}")
        accepted = [ev for ev, r in records if r.decision is Decision.ACCEPTED]
        expected_a = sum(e.delta for e in accepted if e.account_id == "A")
        expected_b = sum(e.delta for e in accepted if e.account_id == "B")
        self.assertAlmostEqual(occ["per_account"]["A"], expected_a)
        self.assertAlmostEqual(occ["per_account"]["B"], expected_b)
        self.assertAlmostEqual(occ["used"], expected_a + expected_b)
        self.assertAlmostEqual(occ["remaining"], 100.0 - expected_a - expected_b)

        # 收敛后重放：通过的变 DUPLICATE，拒绝的仍按相同层级原因拒绝
        for ev, rec in records:
            again = svc.submit_event(ev)
            if rec.decision is Decision.ACCEPTED:
                self.assertIs(again.decision, Decision.DUPLICATE)
            else:
                self.assertIs(again.decision, Decision.REJECTED)
                self.assertIs(again.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
                self.assertIs(again.level, LimitLevel.GROUP)
        self.assertAlmostEqual(svc.group_exposure("G"), 100.0)
        print("[replay] 收敛后结论与拒绝层级稳定")

    def test_reject_level_is_distinguishable(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 50.0))
        svc.set_limit(LimitRule("B", 50.0))
        svc.set_group_limit(GroupRule("G", 80.0))
        svc.assign_account(GroupMembership("A", "G"))
        svc.assign_account(GroupMembership("B", "G"))

        svc.submit_event(ExposureEvent("a1", "A", 1, 50.0))
        svc.submit_event(ExposureEvent("b1", "B", 1, 30.0))  # 组已用 80

        # 再给 A 加 10：账户层（50 上限）先放不下
        r_account = svc.submit_event(ExposureEvent("a2", "A", 2, 10.0))
        # 给 B 加 10：账户层放得下（30+10<=50），组层放不下（80+10>80）
        r_group = svc.submit_event(ExposureEvent("b2", "B", 2, 10.0))
        print(f"[account-level reject] {r_account.reason.value}/{r_account.level.value}")
        print(f"[group-level reject] {r_group.reason.value}/{r_group.level.value}")

        self.assertIs(r_account.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertIs(r_account.level, LimitLevel.ACCOUNT)
        self.assertIs(r_group.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        self.assertIs(r_group.level, LimitLevel.GROUP)

    def test_uncapped_group_member_has_no_group_check(self):
        """未配组限额或未入组的账户保持单账户兼容语义。"""
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.set_group_limit(GroupRule("G", 1000.0))  # 限额存在但 A 未入组
        rec = svc.submit_event(ExposureEvent("a1", "A", 1, 60.0))
        self.assertIs(rec.decision, Decision.ACCEPTED)
        self.assertIsNone(svc.groups.group_of("A"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
