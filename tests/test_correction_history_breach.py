"""更正的“逐生效时点”限额边界：更正后最新总额不超限，但账户层或账户组层
在某历史生效时点曾超限——此类更正必须按对应层级拒绝且不留任何已生效痕迹。

覆盖：
- 账户层历史时点超限（后续扣减把最新总额拉回上限内也不接受）；
- 两账户共享组额度时组层历史时点超限（含跨账户合并时序）；
- 拒绝后账户/账户组金额、占用明细、既有通过或拒绝结论全部不变；
- 调小、撤销、最终确实不超限的合法更正不被误伤；
- 重复、乱序交错到达、未知/非法更正结论与单次提交口径一致；
- 并发提交不出现“先接受、后越限”的中间状态。
"""
import threading
import unittest
from collections import Counter

from app.models import (
    CorrectionEvent,
    Decision,
    ExposureEvent,
    GroupLimitRule,
    LimitRule,
    RejectReason,
)
from app.service import RiskService


class AccountHistoryBreachTest(unittest.TestCase):
    def make_service(self):
        """e1=+100（上限100，通过），e2=-60 扣减 -> 最新敞口 40。"""
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        r1 = svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        r2 = svc.submit_event(ExposureEvent("e2", "A", 2, -60.0))
        self.assertIs(r1.decision, Decision.ACCEPTED)
        self.assertIs(r2.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 40.0)
        return svc

    def test_breaching_history_point_rejected_though_latest_within_cap(self):
        svc = self.make_service()
        # e1 改 120：路径 120（历史时点超上限100）-> 60（最新不超）
        rec = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=120.0))
        print(f"[hist] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.LIMIT_EXCEEDED)

    def test_rejected_correction_leaves_no_trace(self):
        svc = self.make_service()
        svc.set_group_limit(GroupLimitRule("G", 1000.0))
        svc.assign_account("A", "G")
        rec = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=120.0))
        self.assertIs(rec.decision, Decision.REJECTED)
        # 账户金额、组金额、占用明细完全不变
        self.assertEqual(svc.exposure("A"), 40.0)
        self.assertEqual(svc.group_exposure("G"), 40.0)
        usage = svc.group_usage("G")
        self.assertEqual(usage["used"], 40.0)
        self.assertEqual(usage["remaining"], 960.0)
        self.assertEqual(usage["members"], [{"account_id": "A", "exposure": 40.0}])
        # 更正未登记：重复提交仍是同一拒绝结论（不会变成 DUPLICATE/CONFLICT）
        again = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=120.0))
        print(f"[retry] {again.decision.value} {again.reason.value}")
        self.assertIs(again.decision, Decision.REJECTED)
        self.assertIs(again.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertIsNone(svc.store.correction_of("c1"))
        self.assertIsNone(svc.store.correction_for_target("e1"))
        # 原始事件集合不变；合法的改小更正仍可接受
        self.assertEqual([e.event_id for e in svc.events_of("A")], ["e1", "e2"])
        ok = svc.submit_correction(CorrectionEvent("c2", "e1", new_delta=90.0))
        self.assertIs(ok.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 30.0)

    def test_downsize_cancel_and_within_cap_not_falsely_rejected(self):
        # 调小：任何历史时点都不超过原已接受口径，必须通过
        svc = self.make_service()
        r = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=80.0))
        self.assertIs(r.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 20.0)  # 80 - 60(截断前：80-60=20)

        # 撤销：生效 delta=0，路径 0 -> 0
        svc = self.make_service()
        r = svc.submit_correction(CorrectionEvent("c1", "e1", cancel=True))
        self.assertIs(r.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 0.0)

        # 改大但各时点恰好不超限：路径 100 -> 40（100 在上限边界，允许）
        svc = self.make_service()
        r = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=100.0))
        self.assertIs(r.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 40.0)


class GroupHistoryBreachTest(unittest.TestCase):
    def _two_account_base(self, cap: float = 200.0):
        """两账户 A/B 共享组上限 200：
        合并时序 (seq,account,event)：A@1=+100, B@1=+100（组200，恰好满）,
        A@2=-50（组150）。最新组敞口 150。
        """
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", cap))
        svc.assign_account("A", "G")
        svc.assign_account("B", "G")
        records = [
            svc.submit_event(ExposureEvent("a1", "A", 1, 100.0)),
            svc.submit_event(ExposureEvent("b1", "B", 1, 100.0)),
            svc.submit_event(ExposureEvent("a2", "A", 2, -50.0)),
        ]
        for r in records:
            self.assertIs(r.decision, Decision.ACCEPTED)
        self.assertEqual(svc.group_exposure("G"), 150.0)
        return svc

    def test_group_history_point_breach_two_shared_accounts(self):
        svc = self._two_account_base()
        # B 的 b1 改 150：B@1 时点 A 已发生 +100，组=250 超限；
        # 最新组 = 100-50+150 = 200 恰好不超
        rec = svc.submit_correction(CorrectionEvent("c1", "b1", new_delta=150.0))
        print(f"[group-hist] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.GROUP_LIMIT_EXCEEDED)

    def test_group_rejection_rolls_back_usage_and_decisions(self):
        svc = self._two_account_base()
        before = svc.group_usage("G")
        rec = svc.submit_correction(CorrectionEvent("c1", "b1", new_delta=150.0))
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        # 组与各账户金额、占用明细完全不变
        self.assertEqual(svc.exposure("A"), 50.0)
        self.assertEqual(svc.exposure("B"), 100.0)
        self.assertEqual(svc.group_exposure("G"), 150.0)
        self.assertEqual(svc.group_usage("G"), before)
        # 更正未登记，重复提交口径一致，且不被当成冲突更正
        again = svc.submit_correction(CorrectionEvent("c1", "b1", new_delta=150.0))
        self.assertIs(again.decision, Decision.REJECTED)
        self.assertIs(again.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        self.assertIsNone(svc.store.correction_of("c1"))
        # 既有判定不被改写：原事件重放仍是 DUPLICATE
        for ev in (ExposureEvent("a1", "A", 1, 100.0),
                   ExposureEvent("b1", "B", 1, 100.0),
                   ExposureEvent("a2", "A", 2, -50.0)):
            self.assertIs(svc.submit_event(ev).decision, Decision.DUPLICATE)
        self.assertEqual(svc.group_exposure("G"), 150.0)

    def test_account_level_breach_takes_precedence_over_group(self):
        # 账户上限 120 先被触发（组额度充足）：层级必须标成账户级
        svc = self._two_account_base(cap=1000.0)
        svc.set_limit(LimitRule("B", 120.0))
        rec = svc.submit_correction(CorrectionEvent("c1", "b1", new_delta=150.0))
        print(f"[level] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertEqual(svc.exposure("B"), 100.0)
        self.assertEqual(svc.group_exposure("G"), 150.0)

    def test_legal_group_corrections_accepted(self):
        # 改小（B:100->80）与撤销均不误伤
        svc = self._two_account_base()
        r = svc.submit_correction(CorrectionEvent("c1", "b1", new_delta=80.0))
        self.assertIs(r.decision, Decision.ACCEPTED)
        self.assertEqual(svc.group_exposure("G"), 130.0)  # A=50, B=80

        svc = self._two_account_base()
        r = svc.submit_correction(CorrectionEvent("c1", "a1", cancel=True))
        self.assertIs(r.decision, Decision.ACCEPTED)
        # a1 撤销后路径：a2=-50 在 0 上截断 -> A=0, B=100
        self.assertEqual(svc.exposure("A"), 0.0)
        self.assertEqual(svc.group_exposure("G"), 100.0)

    def test_breach_via_other_member_interleaving(self):
        """更正目标是早期事件、超限由其后同组他账户事件叠加造成：
        A@1=+100, B@1=+60（组160）, B@2=+40（组200）, B@3=-70（组130）；
        把 a1 改成 130：B@2 时点组 = 130+60+40 = 230 超 200；
        最新组 = 130 + max(0,60+40-70) = 160 不超。
        """
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 200.0))
        svc.assign_account("A", "G")
        svc.assign_account("B", "G")
        for ev in (ExposureEvent("a1", "A", 1, 100.0),
                   ExposureEvent("b1", "B", 1, 60.0),
                   ExposureEvent("b2", "B", 2, 40.0),
                   ExposureEvent("b3", "B", 3, -70.0)):
            self.assertIs(svc.submit_event(ev).decision, Decision.ACCEPTED)
        self.assertEqual(svc.group_exposure("G"), 130.0)
        rec = svc.submit_correction(CorrectionEvent("c1", "a1", new_delta=130.0))
        print(f"[interleave-hist] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        self.assertEqual(svc.group_exposure("G"), 130.0)
        self.assertIsNone(svc.store.correction_of("c1"))


class InterleavedArrivalTest(unittest.TestCase):
    def test_interleaved_illegal_then_legal_correction_converges(self):
        """更正与原始上报、扣减交错到达：超限更正始终拒绝；待状态变化使其
        合法后，另一条更正可以被接受；未知目标先到的合法更重在收敛后生效。"""
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        # 交错：e1 -> 超限更正（拒绝）-> e2 扣减 -> 同一更正重放（仍拒绝）
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        bad = CorrectionEvent("c-bad", "e1", new_delta=120.0)
        r1 = svc.submit_correction(bad)
        r2 = svc.submit_event(ExposureEvent("e2", "A", 2, -60.0))
        r3 = svc.submit_correction(bad)  # 即使最新已回落到 40，历史口径仍拒绝
        self.assertIs(r1.decision, Decision.REJECTED)
        self.assertIs(r2.decision, Decision.ACCEPTED)
        self.assertIs(r3.decision, Decision.REJECTED)
        self.assertIs(r3.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertEqual(svc.exposure("A"), 40.0)
        # 未知目标先到：合法更正先因 UNKNOWN_TARGET_EVENT 被拒，事件到位后生效
        early = CorrectionEvent("c-early", "e3", new_delta=10.0)
        r4 = svc.submit_correction(early)
        self.assertIs(r4.reason, RejectReason.UNKNOWN_TARGET_EVENT)
        svc.submit_event(ExposureEvent("e3", "A", 3, 50.0))
        r5 = svc.submit_correction(early)
        print(f"[converge] {r5.decision.value} {r5.reason.value}")
        self.assertIs(r5.decision, Decision.ACCEPTED)
        # 路径 100 -> 40 -> 50（e2 在 100 上 -60 = 40；e3 生效 delta=10 -> 50）
        self.assertEqual(svc.exposure("A"), 50.0)
        # 已稳定结论重放不变
        self.assertIs(svc.submit_correction(bad).reason, RejectReason.LIMIT_EXCEEDED)
        self.assertIs(svc.submit_correction(early).decision, Decision.DUPLICATE)

    def test_arrival_order_independent_for_accepted_corrections(self):
        """合法更正集合在不同交错顺序下收敛到同一敞口（口径一致）。"""
        c1 = CorrectionEvent("c1", "e1", new_delta=60.0)
        c2 = CorrectionEvent("c2", "e2", cancel=True)
        orders = [
            [("ev", ExposureEvent("e1", "A", 1, 100.0)),
             ("ev", ExposureEvent("e2", "A", 2, 50.0)), ("co", c1), ("co", c2)],
            [("co", c1), ("ev", ExposureEvent("e1", "A", 1, 100.0)),
             ("co", c2), ("ev", ExposureEvent("e2", "A", 2, 50.0))],
            [("ev", ExposureEvent("e1", "A", 1, 100.0)), ("co", c1),
             ("ev", ExposureEvent("e2", "A", 2, 50.0)), ("co", c2)],
        ]
        finals = []
        for order in orders:
            svc = RiskService()
            svc.set_limit(LimitRule("A", 10_000.0))
            pending = []
            for kind, item in order:
                rec = (svc.submit_correction(item) if kind == "co"
                       else svc.submit_event(item))
                if kind == "co" and rec.decision is Decision.REJECTED:
                    pending.append(item)
            for item in pending:  # 收敛重放
                self.assertIn(svc.submit_correction(item).decision,
                              (Decision.ACCEPTED, Decision.DUPLICATE))
            for item in (c1, c2):  # 再重放一次必须全部幂等
                self.assertIs(svc.submit_correction(item).decision,
                              Decision.DUPLICATE)
            finals.append(svc.exposure("A"))
        print(f"[orders] finals={finals}")
        self.assertEqual(finals, [60.0, 60.0, 60.0])


class ConcurrentCorrectionTest(unittest.TestCase):
    def test_concurrent_breaching_corrections_never_visible(self):
        # 10 个线程并发把同一早期事件改成超限值：全部拒绝，任何查询时点都
        # 看不到已生效的超限口径
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        svc.submit_event(ExposureEvent("e2", "A", 2, -60.0))
        records: list = []
        rec_lock = threading.Lock()
        stop = threading.Event()

        def observer():
            while not stop.is_set():
                # 任何中间观察都必须满足“已登记口径不超限”
                self.assertLessEqual(svc.exposure("A"), 100.0)

        def worker(i):
            rec = svc.submit_correction(
                CorrectionEvent(f"c{i}", "e1", new_delta=120.0))
            with rec_lock:
                records.append(rec)

        obs = threading.Thread(target=observer)
        obs.start()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stop.set()
        obs.join()

        counts = Counter(r.decision for r in records)
        print(f"[concurrent] {dict(counts)} exposure={svc.exposure('A')}")
        # 至多一条进入冲突判定路径；这里 target 相同但 correction_id 各不相同，
        # 第一条若被接受其余会变成“目标已更正”——因此正确口径是：限额先拦截，
        # 全部 LIMIT_EXCEEDED，不存在任何一条 ACCEPTED。
        self.assertEqual(counts[Decision.ACCEPTED], 0)
        self.assertTrue(all(r.reason is RejectReason.LIMIT_EXCEEDED
                            for r in records if r.decision is Decision.REJECTED))
        self.assertEqual(svc.exposure("A"), 40.0)
        self.assertIsNone(svc.store.correction_for_target("e1"))

    def test_concurrent_mixed_corrections_and_events_serializable(self):
        # 并发混合：合法改小更正 + 合法扣减事件 + 超限更正，最终态必合法且确定
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))

        def submit_bad():
            return svc.submit_correction(
                CorrectionEvent("c-bad", "e1", new_delta=150.0))

        def submit_good():
            return svc.submit_correction(
                CorrectionEvent("c-good", "e1", new_delta=40.0))

        def submit_deduction():
            return svc.submit_event(ExposureEvent("e2", "A", 2, -10.0))

        results = []
        barrier = threading.Barrier(3)

        def run(fn):
            barrier.wait()
            results.append(fn())

        ts = [threading.Thread(target=run, args=(f,))
              for f in (submit_bad, submit_good, submit_deduction)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        reasons = Counter((r.decision, r.reason) for r in results)
        print(f"[mixed] {dict(reasons)} exposure={svc.exposure('A')}")
        # 超限更正永远不被接受；口径始终在限额内
        good = svc.store.correction_for_target("e1")
        self.assertLessEqual(svc.exposure("A"), 100.0)
        if good is not None:
            self.assertEqual(good.correction_id, "c-good")
        # 坏更正在任何确定调度下都拒绝（若好更正先生效，则为“目标已更正”
        # 的 INVALID_CORRECTION；否则为 LIMIT_EXCEEDED）——绝不静默改写
        bad_rec = submit_bad()
        self.assertIs(bad_rec.decision, Decision.REJECTED)
        self.assertIn(bad_rec.reason,
                      (RejectReason.LIMIT_EXCEEDED, RejectReason.INVALID_CORRECTION))


if __name__ == "__main__":
    unittest.main(verbosity=2)
