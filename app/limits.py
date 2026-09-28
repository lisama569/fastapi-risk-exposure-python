"""限额校验与并发提交。

所有“校验 + 提交”在同一把锁内完成：并发请求被串行化，
限额不会被突破；重复事件返回稳定的 DUPLICATE 结论且不改状态。

层级规则：事件提交先过账户级上限，再过所属账户组的整体上限，
任意一层放不下即拒绝，拒绝原因可区分（LIMIT_EXCEEDED /
GROUP_LIMIT_EXCEEDED）。账户组敞口 = 组内各账户当前敞口之和。

更正（冲正）：更正按 correction_id 幂等、按 target_event_id 引用原始
上报；同一目标事件至多被更正一次。更正会改变历史事件的生效 delta，
因此不能只校验“更正后的最新总敞口”：只要更正后的生效轨迹在任一时点
（账户层）或任一可能的生效交错时点（账户组层）越过上限，即使最新总额
已被后续扣减拉回上限内，也必须整体回滚并以对应层级原因拒绝。
"""
from __future__ import annotations

import math
import threading

from app.models import (
    CorrectionEvent,
    Decision,
    DecisionRecord,
    ExposureEvent,
    GroupLimitRule,
    LimitRule,
    RejectReason,
)
from app.store import EventStore

# 浮点比较容差：恰好等于上限视为放得下
_EPS = 1e-9


def _fmt(value: float) -> str:
    """判定明细里的金额格式化，去掉浮点尾差（如 80.00000000000001）。"""
    return f"{round(value, 6):g}"


class LimitChecker:
    """账户级 + 账户组级两层限额的校验器。"""

    def __init__(self, store: EventStore) -> None:
        self._lock = threading.RLock()
        self._store = store
        self._rules: dict[str, LimitRule] = {}
        self._group_rules: dict[str, GroupLimitRule] = {}
        # account_id -> group_id（一个账户至多属于一个组，保证归属确定）
        self._membership: dict[str, str] = {}

    # ---- 限额与归属配置 ----

    def set_limit(self, rule: LimitRule) -> None:
        self._validate_cap(rule.max_exposure)
        with self._lock:
            self._rules[rule.account_id] = rule

    def set_group_limit(self, rule: GroupLimitRule) -> None:
        self._validate_cap(rule.max_exposure)
        with self._lock:
            self._group_rules[rule.group_id] = rule

    def assign_account(self, account_id: str, group_id: str) -> None:
        if not account_id or not group_id:
            raise ValueError("account_id/group_id 不能为空")
        with self._lock:
            self._membership[account_id] = group_id

    @staticmethod
    def _validate_cap(cap: float) -> None:
        if cap < 0 or not math.isfinite(cap):
            raise ValueError(f"非法限额: {cap}")

    # ---- 只读查询 ----

    def limit_of(self, account_id: str) -> LimitRule | None:
        with self._lock:
            return self._rules.get(account_id)

    def group_of(self, account_id: str) -> str | None:
        with self._lock:
            return self._membership.get(account_id)

    def group_members(self, group_id: str) -> list[str]:
        with self._lock:
            return sorted(a for a, g in self._membership.items() if g == group_id)

    def group_exposure(self, group_id: str) -> float:
        """账户组敞口 = 组内各账户当前敞口之和（确定性）。"""
        with self._lock:
            return sum(self._store.exposure(a) for a in self.group_members(group_id))

    def group_usage(self, group_id: str) -> dict:
        """额度占用明细：逐账户占用、总占用与剩余额度。"""
        with self._lock:
            members = [
                {"account_id": a, "exposure": self._store.exposure(a)}
                for a in self.group_members(group_id)
            ]
            used = sum(m["exposure"] for m in members)
            rule = self._group_rules.get(group_id)
            limit = rule.max_exposure if rule is not None else None
            return {
                "group_id": group_id,
                "limit": limit,
                "used": used,
                "remaining": None if limit is None else limit - used,
                "members": members,
            }

    # ---- 事件提交 ----

    def check_and_commit(self, event: ExposureEvent) -> DecisionRecord:
        """幂等提交事件并做两级限额校验。

        判定顺序（均在锁内，结论可复现）：
        1. 非法事件 -> REJECTED / INVALID_EVENT；
        2. 重复 event_id -> DUPLICATE（不改状态）；
        3. 账户级超限 -> REJECTED / LIMIT_EXCEEDED（不提交）；
        4. 账户组级超限 -> REJECTED / GROUP_LIMIT_EXCEEDED（不提交）；
        5. 否则 ACCEPTED。
        """
        with self._lock:
            invalid = self._validate(event)
            if invalid is not None:
                return invalid
            if any(e.event_id == event.event_id for e in self._store.events_of(event.account_id)):
                return DecisionRecord(
                    Decision.DUPLICATE, RejectReason.NONE,
                    f"事件 {event.event_id} 已提交，幂等忽略",
                )
            current = self._store.exposure(event.account_id)
            projected = max(0.0, current + event.delta)
            rule = self._rules.get(event.account_id)
            if rule is not None and projected > rule.max_exposure:
                return DecisionRecord(
                    Decision.REJECTED, RejectReason.LIMIT_EXCEEDED,
                    f"账户 {event.account_id} 预计敞口 {projected} 超过账户级上限 "
                    f"{rule.max_exposure}",
                )
            group_id = self._membership.get(event.account_id)
            if group_id is not None:
                group_rule = self._group_rules.get(group_id)
                if group_rule is not None:
                    increment = projected - current
                    group_projected = self.group_exposure(group_id) + increment
                    if group_projected > group_rule.max_exposure:
                        return DecisionRecord(
                            Decision.REJECTED, RejectReason.GROUP_LIMIT_EXCEEDED,
                            f"账户组 {group_id} 预计敞口 {group_projected} 超过组级上限 "
                            f"{group_rule.max_exposure}（账户级检查已通过）",
                        )
            self._store.add(event)
            return DecisionRecord(
                Decision.ACCEPTED, RejectReason.NONE,
                f"事件 {event.event_id} 已接受，账户 {event.account_id} 当前敞口 "
                f"{self._store.exposure(event.account_id)}",
            )

    # ---- 更正（冲正）提交 ----

    def check_and_commit_correction(self, corr: CorrectionEvent) -> DecisionRecord:
        """幂等提交更正。

        判定顺序（均在锁内）：
        1. 字段非法 -> REJECTED / INVALID_CORRECTION；
        2. correction_id 重复：内容一致 -> DUPLICATE；内容冲突 ->
           REJECTED / INVALID_CORRECTION（结论不再确定，必须显式拒绝）；
        3. 目标事件不存在 -> REJECTED / UNKNOWN_TARGET_EVENT；
        4. 目标事件已被其他更正更正过 -> REJECTED / INVALID_CORRECTION；
        5. 试算后账户级/组级在任一历史生效时点超限（即使最新总额已回落）
           -> 回滚并 REJECTED / 对应原因；
        6. 否则 ACCEPTED。
        """
        with self._lock:
            invalid = self._validate_correction(corr)
            if invalid is not None:
                return invalid
            existing = self._store.correction_of(corr.correction_id)
            if existing is not None:
                if existing == corr:
                    return DecisionRecord(
                        Decision.DUPLICATE, RejectReason.NONE,
                        f"更正 {corr.correction_id} 已提交，幂等忽略",
                    )
                return DecisionRecord(
                    Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                    f"更正 {corr.correction_id} 与已登记更正内容冲突，拒绝以避免口径不确定",
                )
            target = self._store.find_event(corr.target_event_id)
            if target is None:
                return DecisionRecord(
                    Decision.REJECTED, RejectReason.UNKNOWN_TARGET_EVENT,
                    f"更正引用的原始事件 {corr.target_event_id} 不存在",
                )
            if self._store.correction_for_target(corr.target_event_id) is not None:
                return DecisionRecord(
                    Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                    f"事件 {corr.target_event_id} 已被更正过，不接受第二条更正",
                )
            # 试算：先记录更正前基线轨迹峰值，再应用更正逐时点校验两级
            # 限额。只有“更正把历史时点抬过上限”才拒绝并回滚；调小/撤销
            # 只会逐时点压低轨迹，绝不误伤；历史口径与最新敞口都不变。
            baseline = self._trajectory_peaks(target.account_id)
            self._store.add_correction(corr)
            breach = self._correction_breach_record(target.account_id, baseline)
            if breach is not None:
                self._store.remove_correction(corr.correction_id)
                return breach
            return DecisionRecord(
                Decision.ACCEPTED, RejectReason.NONE,
                f"更正 {corr.correction_id} 已生效，账户 {target.account_id} 当前敞口 "
                f"{self._store.exposure(target.account_id)}",
            )

    def _trajectory_peaks(self, account_id: str) -> tuple[float, float | None]:
        """记录更正前的轨迹峰值：(账户层逐时点峰值, 组层最小可达峰值或 None)。

        组层只在账户归属了已配置上限的组时计算。
        """
        account_peak = max(self._store.effective_path(account_id), default=0.0)
        group_id = self._membership.get(account_id)
        group_peak: float | None = None
        if group_id is not None and group_id in self._group_rules:
            group_peak = self._group_peak_exposure(group_id)
        return account_peak, group_peak

    def _correction_breach_record(
        self, account_id: str, baseline: tuple[float, float | None]
    ) -> DecisionRecord | None:
        """更正试算：逐时点校验账户层、再校验账户组层历史轨迹。

        判定边界——把更正后的轨迹峰值与上限、更正前基线峰值同时比较：
        - 第一层（账户级）：更正后账户按序去重的每个生效时点敞口都不得
          *超过账户上限且高于基线峰值*。后续扣减把最新总额拉回上限内，
          不能洗白更正把某个历史时点抬过账户上限的事实；而调小/撤销只会
          逐时点压低轨迹，峰值不增，不会被误伤。
        - 第二层（账户组级）：组内各账户的事件可交错生效，按“可交错但
          每账户保序”计算组共享敞口的最小可达峰值；若该峰值被更正抬过
          组上限，则任何交错顺序都必然在某个时点超限，拒绝；存在全程不
          超限的交错顺序（含最终确实不超限）则接受。
        """
        base_account_peak, base_group_peak = baseline
        rule = self._rules.get(account_id)
        if rule is not None:
            for point in self._store.effective_path(account_id):
                if point > rule.max_exposure + _EPS and point > base_account_peak + _EPS:
                    return DecisionRecord(
                        Decision.REJECTED, RejectReason.LIMIT_EXCEEDED,
                        f"更正后账户 {account_id} 在某生效时点的敞口 {_fmt(point)} "
                        f"超过账户级上限 {rule.max_exposure}（即使最新总额已回落），"
                        f"更正已回滚",
                    )
        group_id = self._membership.get(account_id)
        if group_id is not None:
            group_rule = self._group_rules.get(group_id)
            if group_rule is not None:
                peak = self._group_peak_exposure(group_id)
                raised = base_group_peak is None or peak > base_group_peak + _EPS
                if raised and peak > group_rule.max_exposure + _EPS:
                    return DecisionRecord(
                        Decision.REJECTED, RejectReason.GROUP_LIMIT_EXCEEDED,
                        f"更正后账户组 {group_id} 在某生效时点的共享敞口 "
                        f"{_fmt(peak)} 超过组级上限 {group_rule.max_exposure}"
                        f"（即使最新总额已回落），更正已回滚",
                    )
        return None

    def _group_peak_exposure(self, group_id: str) -> float:
        """组内各账户步长在“可交错但每账户保序”前提下，组共享敞口的最小
        可达峰值（分支限界精确求解，不是启发式）。

        - 每个账户按自身步长独立做 0 截断，得到逐账户状态序列；交错只影响
          跨账户时点，每账户内部顺序固定；
        - 状态 = 各账户“已走到第几步”的位置向量（位置即决定该账户当前敞口，
          与到达路径无关，故同位置向量只探索一次）；
        - 每步推进一个账户，组总额 = 各账户当前状态之和，记录路径峰值；
          用优先队列按“当前峰值、当前总额”扩展，第一次到达终点即最小可达
          峰值。该峰值 <= 组上限，当且仅当存在全程不超限的交错顺序。
        """
        import heapq

        members = self.group_members(group_id)
        queues: list[list[float]] = []
        for member in members:
            states: list[float] = []
            total = 0.0
            for delta in self._store.effective_deltas(member):
                total = max(0.0, total + delta)
                states.append(total)
            queues.append(states)
        sizes = tuple(len(q) for q in queues)
        if not sizes or all(n == 0 for n in sizes):
            return 0.0

        def total_at(positions: tuple[int, ...]) -> float:
            total = 0.0
            for i, p in enumerate(positions):
                if p > 0:
                    total += queues[i][p - 1]
            return total

        start = (0,) * len(queues)
        goal = sizes
        # 堆元素：(路径峰值, 当前总额, 位置向量)
        heap: list[tuple[float, float, tuple[int, ...]]] = [(0.0, 0.0, start)]
        seen: set[tuple[int, ...]] = {start}
        while heap:
            peak, current, positions = heapq.heappop(heap)
            if positions == goal:
                return peak
            for i, n in enumerate(sizes):
                if positions[i] >= n:
                    continue
                next_positions = positions[:i] + (positions[i] + 1,) + positions[i + 1:]
                if next_positions in seen:
                    continue
                seen.add(next_positions)
                state_i = queues[i][positions[i]]  # 推进后账户 i 的状态
                next_total = current - (
                    queues[i][positions[i] - 1] if positions[i] > 0 else 0.0
                ) + state_i
                next_peak = max(peak, next_total)
                heapq.heappush(heap, (next_peak, next_total, next_positions))
        return 0.0  # 理论不可达：堆空意味着无状态可扩展

    # ---- 校验 ----

    @staticmethod
    def _validate(event: ExposureEvent) -> DecisionRecord | None:
        if not event.event_id or not event.account_id:
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_EVENT, "event_id/account_id 不能为空"
            )
        if event.seq < 0 or not math.isfinite(event.delta):
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_EVENT,
                f"非法事件字段: seq={event.seq}, delta={event.delta}",
            )
        return None

    @staticmethod
    def _validate_correction(corr: CorrectionEvent) -> DecisionRecord | None:
        if not corr.correction_id or not corr.target_event_id:
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                "correction_id/target_event_id 不能为空",
            )
        if corr.cancel and corr.new_delta is not None:
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                "cancel 与 new_delta 互斥，不能同时提供",
            )
        if not corr.cancel and corr.new_delta is None:
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                "必须提供 new_delta 或将 cancel 置为 true",
            )
        if corr.new_delta is not None and not math.isfinite(corr.new_delta):
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                f"非法更正金额: {corr.new_delta}",
            )
        return None
