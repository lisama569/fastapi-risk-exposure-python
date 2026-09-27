"""账户组共享额度：并发不突破、拒绝层级可区分、占用明细与判定结论对账。"""
import threading
import unittest
from collections import Counter

from app.models import Decision, ExposureEvent, GroupLimitRule, LimitRule, RejectReason
from app.service import RiskService


class GroupLimitConcurrencyTest(unittest.TestCase):
    def test_concurrent_commits_never_breach_group_limit(self):
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 150.0))
        svc.set_limit(LimitRule("A", 100.0))
        svc.set_limit(LimitRule("B", 100.0))
        svc.assign_account("A", "G")
        svc.assign_account("B", "G")
        records = []
        lock = threading.Lock()

        def worker(account, i):
            ev = ExposureEvent(f"{account}-e{i}", account, i + 1, 20.0)
            rec = svc.submit_event(ev)
            with lock:
                records.append((ev, rec))

        threads = [
            threading.Thread(target=worker, args=(account, i))
            for account in ("A", "B") for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for ev, rec in sorted(records, key=lambda x: x[0].event_id):
            print(f"[input] {ev.event_id} -> decision={rec.decision.value} "
                  f"reason={rec.reason.value} detail={rec.detail}")
        counts = Counter(r.decision for _, r in records)
        print(f"[summary] {dict(counts)} group_exposure={svc.group_exposure('G')}")
        # 组内共 10 笔 * 20，组上限 150 -> 恰好 7 笔通过（140），3 笔组级拒绝
        self.assertEqual(counts[Decision.ACCEPTED], 7)
        self.assertEqual(counts[Decision.REJECTED], 3)
        self.assertTrue(all(
            r.reason is RejectReason.GROUP_LIMIT_EXCEEDED
            for _, r in records if r.decision is Decision.REJECTED
        ))
        self.assertEqual(svc.group_exposure("G"), 140.0)
        self.assertLessEqual(svc.group_exposure("G"), 150.0)

        # 收敛后重放：结论与拒绝层级稳定，组敞口不变
        for ev, rec in records:
            again = svc.submit_event(ev)
            if rec.decision is Decision.ACCEPTED:
                self.assertIs(again.decision, Decision.DUPLICATE)
            else:
                self.assertIs(again.decision, Decision.REJECTED)
                self.assertIs(again.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        self.assertEqual(svc.group_exposure("G"), 140.0)
        print("[replay] 收敛后重放结论与拒绝层级稳定，组敞口不变")

    def test_reject_level_is_distinguishable(self):
        svc = RiskService()
        # 账户级放不下：账户上限小于事件金额，组额度充足
        svc.set_limit(LimitRule("A", 50.0))
        svc.set_group_limit(GroupLimitRule("G1", 1000.0))
        svc.assign_account("A", "G1")
        # 组级放不下：账户上限充足，组上限不足
        svc.set_limit(LimitRule("B", 1000.0))
        svc.set_group_limit(GroupLimitRule("G2", 100.0))
        svc.assign_account("B", "G2")

        r1 = svc.submit_event(ExposureEvent("a1", "A", 1, 50.0))
        r2 = svc.submit_event(ExposureEvent("a2", "A", 2, 10.0))   # 账户级超限
        r3 = svc.submit_event(ExposureEvent("b1", "B", 1, 100.0))
        r4 = svc.submit_event(ExposureEvent("b2", "B", 2, 1.0))    # 组级超限
        for r in (r1, r2, r3, r4):
            print(f"[decision] {r.decision.value} reason={r.reason.value} "
                  f"detail={r.detail}")
        self.assertIs(r1.decision, Decision.ACCEPTED)
        self.assertIs(r2.decision, Decision.REJECTED)
        self.assertIs(r2.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertIs(r3.decision, Decision.ACCEPTED)
        self.assertIs(r4.decision, Decision.REJECTED)
        self.assertIs(r4.reason, RejectReason.GROUP_LIMIT_EXCEEDED)

    def test_group_usage_reconciles_with_decisions(self):
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 100.0))
        svc.assign_account("A", "G")
        svc.assign_account("B", "G")
        accepted_deltas = {"A": 0.0, "B": 0.0}
        for account, i, delta in [("A", 1, 40.0), ("B", 1, 50.0),
                                  ("A", 2, 30.0),   # 组级拒绝（40+50+30>100）
                                  ("B", 2, 10.0)]:  # 通过（恰好 100）
            rec = svc.submit_event(ExposureEvent(f"{account}-e{i}", account, i, delta))
            print(f"[input] {account} delta={delta} -> {rec.decision.value} "
                  f"reason={rec.reason.value}")
            if rec.decision is Decision.ACCEPTED:
                accepted_deltas[account] += delta

        usage = svc.group_usage("G")
        print(f"[usage] {usage}")
        self.assertEqual(usage["group_id"], "G")
        self.assertEqual(usage["limit"], 100.0)
        # 明细逐账户占用 == 逐笔被接受事件之和 == 账户敞口查询结果
        members = {m["account_id"]: m["exposure"] for m in usage["members"]}
        self.assertEqual(members, {"A": 40.0, "B": 60.0})
        for account, total in accepted_deltas.items():
            self.assertEqual(members[account], total)
            self.assertEqual(members[account], svc.exposure(account))
        # 总占用与剩余额度对账
        self.assertEqual(usage["used"], 100.0)
        self.assertEqual(usage["used"], svc.group_exposure("G"))
        self.assertEqual(usage["remaining"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
