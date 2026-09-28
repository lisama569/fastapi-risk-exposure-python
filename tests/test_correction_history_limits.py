"""更正（冲正）的历史时点限额校验。

核心缺陷回归：更正把一笔已通过的上报改大时，即使后续扣减已把“最新总
敞口”拉回上限内，只要更正后的生效轨迹在任一时点越过账户级/账户组级
上限，就必须以对应层级原因拒绝并整体回滚；调小、撤销或确实放得下（含
可通过交错生效避开峰值）的更正不能被误伤。
"""
import threading
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


class AccountHistoryLimitTest(unittest.TestCase):
    def test_final_total_under_limit_but_history_breached_is_rejected(self):
        # 首笔 100 恰好到上限，随后扣减到 50；把首笔改大为 130 ->
        # 最新总额 80（未超 100），但历史时点曾为 130，必须账户级拒绝。
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        self.assertIs(svc.submit_event(ExposureEvent("e1", "A", 1, 100.0)).decision,
                      Decision.ACCEPTED)
        self.assertIs(svc.submit_event(ExposureEvent("e2", "A", 2, -50.0)).decision,
                      Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 50.0)

        rec = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=130.0))
        print(f"[history-account] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.LIMIT_EXCEEDED)
        # 回滚保证：账户金额、事件集合、既有判定都不变
        self.assertEqual(svc.exposure("A"), 50.0)
        self.assertEqual([e.event_id for e in svc.events_of("A")], ["e1", "e2"])
        self.assertIsNone(svc.store.correction_of("c1"))

    def test_rejected_correction_stays_rejected_on_repeated_submission(self):
        # 已经稳定的拒绝结论不能被后续扣减静默改写；重复提交仍按同一原因拒绝，
        # 也不会因为“曾经提交过”而变成 DUPLICATE（被拒更正从未登记）。
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        svc.submit_event(ExposureEvent("e2", "A", 2, -50.0))
        bad = CorrectionEvent("c1", "e1", new_delta=130.0)
        r1 = svc.submit_correction(bad)
        svc.submit_event(ExposureEvent("e3", "A", 3, -50.0))  # 最新敞口再降
        r2 = svc.submit_correction(bad)
        print(f"[stable-reject] {r1.reason.value} -> {r2.reason.value}")
        for r in (r1, r2):
            self.assertIs(r.decision, Decision.REJECTED)
            self.assertIs(r.reason, RejectReason.LIMIT_EXCEEDED)
        self.assertEqual(svc.exposure("A"), 0.0)

    def test_boundary_at_limit_is_accepted(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        svc.submit_event(ExposureEvent("e2", "A", 2, -50.0))
        # 改大后历史峰值恰好 100 == 上限：放得下
        rec = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=100.0))
        print(f"[boundary] {rec.decision.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 50.0)

    def test_decrease_and_cancel_corrections_not_affected(self):
        # 调小 / 撤销只会逐时点压低轨迹，即使最新总额本来就不超限也正常生效
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        svc.submit_event(ExposureEvent("e2", "A", 2, -50.0))
        down = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=70.0))
        self.assertIs(down.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 20.0)
        cancel = svc.submit_correction(CorrectionEvent("c2", "e2", cancel=True))
        self.assertIs(cancel.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 70.0)

    def test_decrease_accepted_even_if_canonical_history_previously_breached(self):
        # 乱序上报可能使按 seq 重排后的规范轨迹存在超限窟窿（到达顺序本身合法）：
        # 到达 80 -> -80(截断0) -> 迟到的 seq2=40，规范序列为 80,40,-80
        # -> 轨迹 [80,120,40]。对首笔做“调小”的更正不得被这个既有窟窿误伤。
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 80.0))
        svc.submit_event(ExposureEvent("e3", "A", 3, -80.0))
        svc.submit_event(ExposureEvent("e2", "A", 2, 40.0))
        self.assertEqual(svc.exposure("A"), 40.0)  # 到达顺序 80,0,40
        down = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=70.0))
        print(f"[preexisting-hole:down] {down.decision.value} {down.detail}")
        self.assertIs(down.decision, Decision.ACCEPTED)  # 峰值 120 -> 110，未被抬升
        up = svc.submit_correction(CorrectionEvent("c2", "e2", new_delta=60.0))
        print(f"[preexisting-hole:up] {up.decision.value} {up.reason.value}")
        self.assertIs(up.decision, Decision.REJECTED)  # 峰值抬到 130 > 100
        self.assertIs(up.reason, RejectReason.LIMIT_EXCEEDED)


class GroupHistoryLimitTest(unittest.TestCase):
    def test_single_member_group_final_low_but_history_breached(self):
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 100.0))
        svc.assign_account("A", "G")
        svc.submit_event(ExposureEvent("a1", "A", 1, 80.0))
        svc.submit_event(ExposureEvent("a2", "A", 2, -70.0))
        self.assertEqual(svc.group_exposure("G"), 10.0)
        rec = svc.submit_correction(CorrectionEvent("c1", "a1", new_delta=150.0))
        print(f"[history-group] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        self.assertEqual(svc.exposure("A"), 10.0)
        self.assertEqual(svc.group_exposure("G"), 10.0)
        self.assertEqual(svc.group_usage("G")["used"], 10.0)

    def test_two_accounts_unavoidable_peak_rejected(self):
        # A: 60 -> -30（=30），B=60，组上限 100，最终 90 未超限。
        # 把 A 首笔改为 90 后，最优交错为 A 先走 90->60 再让 B 到 60，
        # 最小可达峰值仍是 120（>100），任何顺序都放不下 -> 组级拒绝。
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 100.0))
        svc.assign_account("A", "G")
        svc.assign_account("B", "G")
        svc.submit_event(ExposureEvent("a1", "A", 1, 60.0))
        svc.submit_event(ExposureEvent("a2", "A", 2, -30.0))
        svc.submit_event(ExposureEvent("b1", "B", 1, 60.0))
        self.assertEqual(svc.group_exposure("G"), 90.0)

        rec = svc.submit_correction(CorrectionEvent("c1", "a1", new_delta=90.0))
        print(f"[two-account:peak] {rec.decision.value} {rec.reason.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.GROUP_LIMIT_EXCEEDED)
        # 拒绝后账户/账户组金额、占用明细与既有判定都不变
        self.assertEqual(svc.exposure("A"), 30.0)
        self.assertEqual(svc.exposure("B"), 60.0)
        usage = svc.group_usage("G")
        self.assertEqual(usage["used"], 90.0)
        self.assertEqual(usage["remaining"], 10.0)
        self.assertEqual(
            {m["account_id"]: m["exposure"] for m in usage["members"]},
            {"A": 30.0, "B": 60.0},
        )
        self.assertIsNone(svc.store.correction_of("c1"))

    def test_two_accounts_feasible_interleaving_accepted(self):
        # 同一初始形态，把 A 首笔改为 50：先走完 A（50->20）再让 B 到 60，
        # 组峰值 80 <= 100，存在全程不超限的交错顺序，最终组敞口 80：接受。
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 100.0))
        svc.assign_account("A", "G")
        svc.assign_account("B", "G")
        svc.submit_event(ExposureEvent("a1", "A", 1, 60.0))
        svc.submit_event(ExposureEvent("a2", "A", 2, -30.0))
        svc.submit_event(ExposureEvent("b1", "B", 1, 60.0))
        rec = svc.submit_correction(CorrectionEvent("c1", "a1", new_delta=50.0))
        print(f"[two-account:feasible] {rec.decision.value} {rec.detail}")
        self.assertIs(rec.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 20.0)
        self.assertEqual(svc.group_exposure("G"), 80.0)

    def test_account_level_breach_reported_before_group_level(self):
        # 同一笔更正两层都放不下时，先报账户级原因（层级判定顺序稳定）
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.set_group_limit(GroupLimitRule("G", 100.0))
        svc.assign_account("A", "G")
        svc.submit_event(ExposureEvent("a1", "A", 1, 100.0))
        svc.submit_event(ExposureEvent("a2", "A", 2, -90.0))
        rec = svc.submit_correction(CorrectionEvent("c1", "a1", new_delta=150.0))
        print(f"[level-order] {rec.reason.value}")
        self.assertIs(rec.decision, Decision.REJECTED)
        self.assertIs(rec.reason, RejectReason.LIMIT_EXCEEDED)

    def test_cancel_correction_frees_group_headroom(self):
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 100.0))
        svc.assign_account("A", "G")
        svc.assign_account("B", "G")
        svc.submit_event(ExposureEvent("a1", "A", 1, 90.0))
        svc.submit_event(ExposureEvent("b1", "B", 1, 10.0))  # 组恰好 100
        rec = svc.submit_correction(CorrectionEvent("c1", "a1", cancel=True))
        self.assertIs(rec.decision, Decision.ACCEPTED)
        self.assertEqual(svc.group_exposure("G"), 10.0)
        self.assertEqual(svc.group_usage("G")["remaining"], 90.0)


class CorrectionInterleavingConvergenceTest(unittest.TestCase):
    def test_interleaved_arrivals_converge_to_same_outcome(self):
        # 形态：a1=80（账户上限 100；将被改大为 120，历史峰值 120 -> 账户层
        # 必拒），a2=-70 把最新敞口拉低，b1=60 占用另一个共享账户。组上限放宽
        # 到 200，保证事件在任何到达顺序下都被受理，从而隔离“更正”的收敛口径：
        # 无论更正先到（UNKNOWN_TARGET_EVENT）还是交错到达，收敛重放后坏更正
        # 恒为账户级拒绝且口径稳定，账户/账户组金额一致。
        def run(order):
            svc = RiskService()
            svc.set_limit(LimitRule("A", 100.0))
            svc.set_group_limit(GroupLimitRule("G", 200.0))
            svc.assign_account("A", "G")
            svc.assign_account("B", "G")
            first = []
            for item in order:
                rec = (svc.submit_correction(item)
                       if isinstance(item, CorrectionEvent)
                       else svc.submit_event(item))
                first.append(rec)
            # 收敛重放：未知目标的被拒更正现在可被重新判定
            replayed = svc.submit_correction(CorrectionEvent("c1", "a1", new_delta=120.0))
            again = svc.submit_correction(CorrectionEvent("c1", "a1", new_delta=120.0))
            return svc, first, replayed, again

        a1 = ExposureEvent("a1", "A", 1, 80.0)
        a2 = ExposureEvent("a2", "A", 2, -70.0)
        b1 = ExposureEvent("b1", "B", 1, 60.0)
        c1 = CorrectionEvent("c1", "a1", new_delta=120.0)
        orders = [[a1, a2, b1, c1], [c1, a1, b1, a2], [a1, c1, a2, b1]]
        finals = []
        for order in orders:
            svc, first, replayed, again = run(order)
            print(f"[interleave] first={[r.reason.value for r in first]} "
                  f"replay={replayed.reason.value} again={again.reason.value} "
                  f"A={svc.exposure('A')} B={svc.exposure('B')} G={svc.group_exposure('G')}")
            self.assertIs(replayed.decision, Decision.REJECTED)
            self.assertIs(replayed.reason, RejectReason.LIMIT_EXCEEDED)
            self.assertIs(again.decision, Decision.REJECTED)
            self.assertIs(again.reason, RejectReason.LIMIT_EXCEEDED)
            finals.append((svc.exposure("A"), svc.exposure("B"),
                           svc.group_exposure("G"),
                           len(svc.events_of("A"))))
        # 三种到达顺序收敛到同一口径：A=10, B=60, 组=70，更正未生效
        self.assertEqual(finals, [(10.0, 60.0, 70.0, 2)] * len(orders))

    def test_rejected_breaching_correction_does_not_block_later_valid_one(self):
        svc = RiskService()
        svc.set_limit(LimitRule("A", 100.0))
        svc.submit_event(ExposureEvent("e1", "A", 1, 100.0))
        svc.submit_event(ExposureEvent("e2", "A", 2, -50.0))
        bad = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=130.0))
        self.assertIs(bad.reason, RejectReason.LIMIT_EXCEEDED)
        # 回滚干净：另一条合法更正仍可走完全部判定路径
        ok = svc.submit_correction(CorrectionEvent("c2", "e1", new_delta=90.0))
        print(f"[after-rollback] {ok.decision.value} {ok.detail}")
        self.assertIs(ok.decision, Decision.ACCEPTED)
        self.assertEqual(svc.exposure("A"), 40.0)
        # 被拒更正的 id 未被登记，重放仍是重新判定而不是 DUPLICATE
        replay = svc.submit_correction(CorrectionEvent("c1", "e1", new_delta=130.0))
        self.assertIs(replay.decision, Decision.REJECTED)
        self.assertIs(replay.reason, RejectReason.INVALID_CORRECTION)  # 目标已被 c2 更正


class CorrectionConcurrencyTest(unittest.TestCase):
    def test_concurrent_corrections_and_events_never_leave_over_limit(self):
        # 初始：A: 60 -> -30（=30），B=60，组=90，组上限 100。
        # 坏更正把 A 首笔改为 90：A[90,60] 与 B[60] 的最小可达峰值 120，
        #   无论如何交错都放不下 -> 只要先于好更正进锁就必被组级拒绝；
        # 好更正把 A 首笔改为 50：A[50,20]，最小峰值 80 -> 必受，只生效一次；
        # b2 给 B +20：仅当好更正先生效（组 80）时放得下（组恰好 100）。
        svc = RiskService()
        svc.set_group_limit(GroupLimitRule("G", 100.0))
        svc.assign_account("A", "G")
        svc.assign_account("B", "G")
        svc.submit_event(ExposureEvent("a1", "A", 1, 60.0))
        svc.submit_event(ExposureEvent("a2", "A", 2, -30.0))  # A=30
        svc.submit_event(ExposureEvent("b1", "B", 1, 60.0))   # 组=90

        bad = CorrectionEvent("c-bad", "a1", new_delta=90.0)
        good = CorrectionEvent("c-good", "a1", new_delta=50.0)
        more_b = ExposureEvent("b2", "B", 2, 20.0)
        results = []
        observed = []  # 每次决策在锁外随即观察到的组敞口
        lock = threading.Lock()

        def submit(fn, payload):
            rec = fn(payload)
            snapshot = svc.group_exposure("G")
            with lock:
                results.append((payload, rec))
                observed.append(snapshot)

        jobs = [
            (svc.submit_correction, bad),
            (svc.submit_correction, good),
            (svc.submit_event, more_b),
        ]
        threads = []
        for _ in range(2):  # 两轮并发，重复提交验证幂等口径稳定
            for fn, payload in jobs:
                threads.append(
                    threading.Thread(target=submit, args=(fn, payload))
                )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        by_id = {}
        for payload, rec in results:
            key = getattr(payload, "correction_id", None) or payload.event_id
            by_id.setdefault(key, []).append(rec)
        for key, recs in by_id.items():
            print(f"[concurrent:{key}] " +
                  ", ".join(f"{r.decision.value}/{r.reason.value}" for r in recs))
        print(f"[state] A={svc.exposure('A')} B={svc.exposure('B')} "
              f"G={svc.group_exposure('G')} events_A="
              f"{[e.event_id for e in svc.events_of('A')]}")

        # 坏更正在任何交错下都必为拒绝：
        #  - 先于好更正进锁 -> 逐时点试算放不下 -> GROUP_LIMIT_EXCEEDED；
        #  - 后于好更正进锁 -> 目标已被更正过 -> INVALID_CORRECTION。
        self.assertTrue(all(r.decision is Decision.REJECTED for r in by_id["c-bad"]))
        self.assertTrue(all(
            r.reason in (RejectReason.GROUP_LIMIT_EXCEEDED,
                         RejectReason.INVALID_CORRECTION)
            for r in by_id["c-bad"]
        ))
        # 好更正只生效一次：一次 ACCEPTED，其余为 DUPLICATE
        good_recs = by_id["c-good"]
        self.assertEqual(
            sum(1 for r in good_recs if r.decision is Decision.ACCEPTED), 1
        )
        self.assertTrue(all(
            r.decision in (Decision.ACCEPTED, Decision.DUPLICATE) for r in good_recs
        ))
        # 任何线程在任何时点观察到的组敞口都不超过上限
        self.assertTrue(all(v <= 100.0 + 1e-9 for v in observed), observed)
        # 终态：A=20（好更正口径）；B=60 或 80（b2 是否排到好更正之前被拒）；
        # 组敞口恒为 80 或恰好 100，坏更正从未生效
        self.assertEqual(svc.exposure("A"), 20.0)
        self.assertIn(svc.exposure("B"), (60.0, 80.0))
        self.assertIn(svc.group_exposure("G"), (80.0, 100.0))
        self.assertLessEqual(svc.group_exposure("G"), 100.0)
        self.assertIsNone(svc.store.correction_of("c-bad"))
        self.assertEqual(len(svc.events_of("A")), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
