"""幂等重放与乱序到达：最终敞口必须等于按序去重后的参考结果。"""
import random
import unittest

from app.models import Decision, ExposureEvent, LimitRule
from app.service import RiskService


def reference_exposure(events):
    """参考实现：按 (seq, event_id) 去重排序后逐步累加、负值截断。"""
    dedup = {e.event_id: e for e in events}
    total = 0.0
    for e in sorted(dedup.values(), key=lambda x: (x.seq, x.event_id)):
        total = max(0.0, total + e.delta)
    return total


class IdempotencyTest(unittest.TestCase):
    def test_replay_and_out_of_order(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 10_000.0))
        base = [
            ExposureEvent(f"e{i}", "A", i, delta)
            for i, delta in enumerate([100.0, -30.0, 50.0, -200.0, 80.0], start=1)
        ]
        # 构造乱序 + 重复投递的到达序列
        arrivals = base[::-1] + base + [base[2], base[0]]
        random.Random(42).shuffle(arrivals)
        for ev in arrivals:
            rec = svc.submit_event(ev)
            print(f"[input] {ev} -> decision={rec.decision.value} "
                  f"reason={rec.reason.value} detail={rec.detail}")
            self.assertIn(rec.decision, (Decision.ACCEPTED, Decision.DUPLICATE))
        expected = reference_exposure(base)
        actual = svc.exposure("A")
        print(f"[assert] expected={expected} actual={actual}")
        self.assertEqual(actual, expected)
        self.assertGreaterEqual(actual, 0.0)

    def test_duplicate_never_double_counts(self):
        svc = RiskService()
        ev = ExposureEvent("dup-1", "B", 1, 40.0)
        for attempt in range(3):
            rec = svc.submit_event(ev)
            print(f"[replay {attempt}] decision={rec.decision.value} "
                  f"reason={rec.reason.value}")
        self.assertEqual(svc.exposure("B"), 40.0)
        self.assertEqual(len(svc.events_of("B")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
