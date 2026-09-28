"""限额校验与并发提交。

所有“校验 + 提交”在同一把锁内完成：并发请求被串行化，
限额不会被突破；重复事件返回稳定的 DUPLICATE 结论且不改状态。

层级规则：事件提交先过账户级上限，再过所属账户组的整体上限，
任意一层放不下即拒绝，拒绝原因可区分（LIMIT_EXCEEDED /
GROUP_LIMIT_EXCEEDED）。账户组敞口 = 组内各账户当前敞口之和。

更正（冲正）：更正按 correction_id 幂等、按 target_event_id 引用原始
上报；同一目标事件至多被更正一次。更正会改变账户/账户组敞口，
因此改大金额的更正同样要做两级限额试算，超限即拒绝。

限额校验的判定边界是“每一个生效时点”，而不是更正后的最新总额：
更正会追溯替换/抹掉历史事件的生效 delta，必须按更正后的序列重放，
任一时点（含历史时点与最新时点）账户级或组级累计敞口超过对应上限即
拒绝。这样“早期上报已通过、后期扣减把敞口拉低、再把早期金额改大到
超限”的更正无法靠后续扣减“洗白”历史超限口径。试算全程只读（不产生
已登记的中间状态），通过后才在同一把锁内登记，故不存在“先接受后越限”
的中间窗口，拒绝也无需回滚。
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
        5. 试算后任一生效时点账户级/组级超限 -> REJECTED / 对应原因
           （试算只读，登记状态从未改变，无需回滚）；
        6. 否则登记更正并 ACCEPTED。
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
            # 只读试算：在逐生效时点路径上检查两级限额，任一点超限即拒绝
            trial_delta = 0.0 if corr.cancel else corr.new_delta
            breach = self._historical_breach_record(
                target.account_id, {corr.target_event_id: trial_delta}
            )
            if breach is not None:
                return breach
            self._store.add_correction(corr)
            return DecisionRecord(
                Decision.ACCEPTED, RejectReason.NONE,
                f"更正 {corr.correction_id} 已生效，账户 {target.account_id} 当前敞口 "
                f"{self._store.exposure(target.account_id)}",
            )

    def _historical_breach_record(
        self,
        account_id: str,
        overrides: dict[str, float],
    ) -> DecisionRecord | None:
        """试算更正后账户在每一个生效时点是否越过账户级/组级上限。

        逐点检查而非只看最新总额：更正追溯改写历史事件的生效 delta，
        即使后续扣减使最新总额回落到上限内，历史生效时点的超限同样拒绝。
        判定层级与事件提交一致：账户级放不下优先 LIMIT_EXCEEDED，
        账户级通过后组级任一点放不下为 GROUP_LIMIT_EXCEEDED。
        """
        rule = self._rules.get(account_id)
        if rule is not None:
            path = self._store.effective_path(account_id, overrides)
            latest = path[-1][1] if path else 0.0
            for ev, total in path:
                if total > rule.max_exposure:
                    return DecisionRecord(
                        Decision.REJECTED, RejectReason.LIMIT_EXCEEDED,
                        f"更正后账户 {account_id} 在生效事件 {ev.event_id} 时点敞口 "
                        f"{total} 超过账户级上限 {rule.max_exposure}（即使更正后最新"
                        f"总额 {latest} 未超限，历史时点超限仍拒绝），更正未生效",
                    )
        group_id = self._membership.get(account_id)
        if group_id is not None:
            group_rule = self._group_rules.get(group_id)
            if group_rule is not None:
                members = self.group_members(group_id)
                overrides_by_account = {account_id: overrides}
                for owner, ev, group_total in self._store.group_effective_path(
                    members, overrides_by_account
                ):
                    if group_total > group_rule.max_exposure:
                        return DecisionRecord(
                            Decision.REJECTED, RejectReason.GROUP_LIMIT_EXCEEDED,
                            f"更正后账户组 {group_id} 在账户 {owner} 生效事件 "
                            f"{ev.event_id} 时点共享敞口 {group_total} 超过组级上限 "
                            f"{group_rule.max_exposure}（最新总额未超限亦拒绝），"
                            f"更正未生效",
                        )
        return None

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
