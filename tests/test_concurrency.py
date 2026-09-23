"""并发限额提交：限额不得突破，收敛后结论稳定、原因可区分。"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

from app.models import LimitCheck, RejectReason
from tests._helpers import fresh_stack, log_case


class ConcurrencyLimitTest(unittest.TestCase):
    def test_parallel_submissions_never_break_limit(self) -> None:
        store, service, _ = fresh_stack()
        store.ensure_account("acct-c", Decimal("1000"))

        # 20 笔各 100，总额 2000 > 限额 1000，恰好 10 笔应通过
        requests = [
            LimitCheck(request_id=f"r{i:02d}", account_id="acct-c",
                       amount=Decimal("100"))
            for i in range(20)
        ]

        def submit(req: LimitCheck) -> object:
            return service.check_limit(req)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(submit, r) for r in requests]
            results = [f.result() for f in as_completed(futures)]

        approved = [r for r in results if r.approved]
        rejected = [r for r in results if not r.approved]
        final_exposure = store.get_exposure("acct-c")
        total_committed = sum((r.exposure_after - r.exposure_before for r in approved),
                              Decimal("0"))

        log_case(
            "20 并发提交 x100，限额 1000",
            [r.request_id for r in requests],
            f"通过 {len(approved)} 笔；拒绝 {len(rejected)} 笔；"
            f"通过合计占用={total_committed}；最终敞口={final_exposure}；"
            f"拒绝原因集合={sorted({r.reason.value for r in rejected})}",
        )

        self.assertEqual(len(approved), 10)
        self.assertEqual(len(rejected), 10)
        self.assertEqual(total_committed, Decimal("1000"))
        self.assertEqual(final_exposure, Decimal("1000"))
        for r in rejected:
            self.assertEqual(r.reason, RejectReason.LIMIT_EXCEEDED)

    def test_convergent_decision_is_stable(self) -> None:
        """每个请求的结论不受并发交错影响；重放同一 request_id 结论稳定。"""
        store, service, _ = fresh_stack()
        store.ensure_account("acct-s", Decimal("500"))
        requests = [
            LimitCheck(request_id=f"s{i}", account_id="acct-s",
                       amount=Decimal("200"))
            for i in range(5)
        ]
        barrier = threading.Barrier(5)

        def submit(req: LimitCheck) -> object:
            barrier.wait()  # 尽量制造同时进入临界区的压力
            return service.check_limit(req)

        with ThreadPoolExecutor(max_workers=5) as pool:
            first = list(pool.map(submit, requests))

        # 串行重放全部 request_id，结论必须与并发首轮完全一致
        second = [service.check_limit(r) for r in requests]
        first_map = {r.request_id: r for r in first}
        second_map = {r.request_id: r for r in second}
        log_case(
            "5 并发 x200，限额 500 + 结论稳定重放",
            [r.request_id for r in requests],
            {rid: f"approved={first_map[rid].approved}->重放={second_map[rid].approved},"
                  f"dup_reason={second_map[rid].reason}"
             for rid in sorted(first_map)},
        )
        approved_ids = sorted(rid for rid, r in first_map.items() if r.approved)
        self.assertEqual(len(approved_ids), 2)
        for rid in first_map:
            self.assertEqual(first_map[rid].approved, second_map[rid].approved)
            self.assertEqual(first_map[rid].reason, second_map[rid].reason)
        self.assertEqual(store.get_exposure("acct-s"), Decimal("400"))

    def test_reject_reasons_are_distinguishable(self) -> None:
        """不同失败路径给出不同拒绝原因。"""
        store, service, _ = fresh_stack()
        store.ensure_account("acct-d", Decimal("100"))

        over_limit = service.check_limit(
            LimitCheck(request_id="q1", account_id="acct-d", amount=Decimal("101")))
        missing_account = service.check_limit(
            LimitCheck(request_id="q2", account_id="ghost", amount=Decimal("1")))
        negative = service.check_limit(
            LimitCheck(request_id="q3", account_id="acct-d", amount=Decimal("-1")))

        store.ensure_account("no-limit")  # 建了基准但没配限额
        no_limit = service.check_limit(
            LimitCheck(request_id="q4", account_id="no-limit", amount=Decimal("1")))

        log_case(
            "拒绝原因可区分",
            "超限101/未知账户ghost/负数-1/未配限额",
            f"q1={over_limit.reason.value}; q2={missing_account.reason.value}; "
            f"q3={negative.reason.value}; q4={no_limit.reason.value}",
        )
        self.assertEqual(over_limit.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertEqual(missing_account.reason, RejectReason.ACCOUNT_NOT_FOUND)
        self.assertEqual(negative.reason, RejectReason.INVALID_AMOUNT)
        self.assertEqual(no_limit.reason, RejectReason.ACCOUNT_NOT_FOUND)
        self.assertIn("未配置限额", no_limit.detail or "")
        self.assertEqual(store.get_exposure("acct-d"), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
