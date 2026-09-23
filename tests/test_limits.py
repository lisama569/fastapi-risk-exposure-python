"""限额校验与并发提交：限额不被突破，收敛后结论稳定且原因可区分。"""
import threading
import unittest
from collections import Counter

from app.models import Decision, ExposureEvent, LimitRule, RejectReason
from app.service import RiskService


class LimitConcurrencyTest(unittest.TestCase):
    def test_concurrent_commits_never_breach_limit(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        records = []
        lock = threading.Lock()

        def worker(i):
            ev = ExposureEvent(f"e{i}", "A", i + 1, 20.0)
            rec = svc.submit_event(ev)
            with lock:
                records.append((ev, rec))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for ev, rec in sorted(records, key=lambda x: x[0].seq):
            print(f"[input] {ev.event_id} delta={ev.delta} -> "
                  f"decision={rec.decision.value} reason={rec.reason.value} "
                  f"detail={rec.detail}")
        counts = Counter(r.decision for _, r in records)
        print(f"[summary] {dict(counts)} final_exposure={svc.exposure('A')}")
        self.assertEqual(counts[Decision.ACCEPTED], 5)
        self.assertEqual(counts[Decision.REJECTED], 5)
        self.assertTrue(all(
            r.reason is RejectReason.LIMIT_EXCEEDED
            for _, r in records if r.decision is Decision.REJECTED
        ))
        self.assertEqual(svc.exposure("A"), 100.0)

        # 收敛后重放：结论稳定（已接受的变 DUPLICATE，被拒绝的重试仍拒绝）
        for ev, rec in records:
            again = svc.submit_event(ev)
            if rec.decision is Decision.ACCEPTED:
                self.assertIs(again.decision, Decision.DUPLICATE)
            else:
                self.assertIs(again.decision, Decision.REJECTED)
                self.assertIs(again.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertEqual(svc.exposure("A"), 100.0)
        print("[replay] 收敛后重放结论稳定，敞口不变")

    def test_reject_reasons_are_distinguishable(self):
        svc = RiskService()
        svc.set_limit(LimitRule("B", 50.0))
        svc.submit_event(ExposureEvent("b0", "B", 1, 50.0))
        cases = [
            ExposureEvent("bad-1", "B", 2, float("nan")),   # 非法事件
            ExposureEvent("b1", "B", 2, 10.0),              # 超限
            ExposureEvent("b2", "B", 2, -10.0),             # 合法扣减
        ]
        reasons = []
        for ev in cases:
            rec = svc.submit_event(ev)
            reasons.append(rec.reason)
            print(f"[input] {ev} -> decision={rec.decision.value} "
                  f"reason={rec.reason.value} detail={rec.detail}")
        self.assertEqual(reasons, [
            RejectReason.INVALID_EVENT,
            RejectReason.LIMIT_EXCEEDED,
            RejectReason.NONE,
        ])
        self.assertEqual(svc.exposure("B"), 40.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
