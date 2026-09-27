"""限额校验与并发提交（账户层 + 账户组层）。

所有“校验 + 提交”在同一把共享锁内完成：并发事件上报被串行化，
账户额度与账户组共享额度都不会被突破；重复事件返回稳定的 DUPLICATE
结论且不改状态。

判定顺序（均在锁内，结论可复现）：
1. 非法事件 -> REJECTED / INVALID_EVENT；
2. 重复 event_id -> DUPLICATE（不改状态）；
3. 提交后账户敞口超限 -> REJECTED / LIMIT_EXCEEDED, level=ACCOUNT；
4. 提交后所属账户组敞口超限 -> REJECTED / GROUP_LIMIT_EXCEEDED, level=GROUP；
5. 否则 ACCEPTED。

校验敞口一律基于“更正后生效视图”：已撤销的事件不计、金额已更正的按
新值计。
"""
from __future__ import annotations

import math
import threading

from app.groups import GroupRegistry
from app.models import (
    Decision,
    DecisionRecord,
    ExposureEvent,
    LimitLevel,
    LimitRule,
    RejectReason,
)
from app.store import DeltaResolver, EventStore

# 容忍浮点误差
_EPS = 1e-9


class LimitChecker:
    """账户级 + 账户组级敞口上限校验器。"""

    def __init__(
        self,
        store: EventStore,
        groups: GroupRegistry,
        lock: threading.RLock | None = None,
    ) -> None:
        self._lock = lock if lock is not None else threading.RLock()
        self._store = store
        self._groups = groups
        self._rules: dict[str, LimitRule] = {}
        self._resolve: DeltaResolver | None = None

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def bind_resolver(self, resolve: DeltaResolver) -> None:
        """注入更正生效视图（由 CorrectionManager 提供）。"""
        self._resolve = resolve

    def resolve_view(self) -> DeltaResolver | None:
        return self._resolve

    def set_limit(self, rule: LimitRule) -> None:
        if rule.max_exposure < 0 or not math.isfinite(rule.max_exposure):
            raise ValueError(f"非法限额: {rule.max_exposure}")
        with self._lock:
            self._rules[rule.account_id] = rule

    def limit_of(self, account_id: str) -> LimitRule | None:
        with self._lock:
            return self._rules.get(account_id)

    def group_exposure(self, group_id: str) -> float:
        """账户组当前生效敞口 = 成员生效敞口之和。"""
        with self._lock:
            return self._compute_group_exposure(group_id, self._resolve)

    def check_and_commit(self, event: ExposureEvent) -> DecisionRecord:
        """幂等提交事件并依次校验账户层、账户组层限额。"""
        with self._lock:
            invalid = self._validate(event)
            if invalid is not None:
                return invalid
            if self._store.account_of(event.event_id) is not None:
                return DecisionRecord(
                    Decision.DUPLICATE, RejectReason.NONE,
                    f"事件 {event.event_id} 已提交，幂等忽略",
                )

            current = self._store.exposure(event.account_id, self._resolve)
            projected = max(0.0, current + event.delta)

            rule = self._rules.get(event.account_id)
            if rule is not None and projected > rule.max_exposure + _EPS:
                return DecisionRecord(
                    Decision.REJECTED, RejectReason.LIMIT_EXCEEDED,
                    f"账户 {event.account_id} 预计敞口 {projected} 超过账户上限 "
                    f"{rule.max_exposure}（账户层放不下）",
                    level=LimitLevel.ACCOUNT,
                )

            group_id = self._groups.group_of(event.account_id)
            if group_id is not None:
                group_rule = self._groups.group_limit(group_id)
                if group_rule is not None:
                    group_total = self._compute_group_exposure(group_id, self._resolve)
                    projected_group = group_total - current + projected
                    if projected_group > group_rule.max_exposure + _EPS:
                        return DecisionRecord(
                            Decision.REJECTED, RejectReason.GROUP_LIMIT_EXCEEDED,
                            f"账户组 {group_id} 预计敞口 {projected_group} 超过组上限 "
                            f"{group_rule.max_exposure}（账户组层放不下）",
                            level=LimitLevel.GROUP,
                        )

            self._store.add(event)
            return DecisionRecord(
                Decision.ACCEPTED, RejectReason.NONE,
                f"事件 {event.event_id} 已接受，账户 {event.account_id} 当前敞口 "
                f"{self._store.exposure(event.account_id, self._resolve)}",
            )

    def validate_correction_view(
        self, resolve: DeltaResolver, account_id: str
    ) -> DecisionRecord | None:
        """更正生效前校验：历史判定在新视图下必须全部仍然成立。

        - 账户层：该账户每一步前缀敞口都不得超过账户上限（更正早期事件
          的金额可能整体抬高后续前缀）；
        - 账户组层：所属组最终总额不得超过组上限。
        任一层放不下即返回对应层级的拒绝记录。
        """
        with self._lock:
            rule = self._rules.get(account_id)
            if rule is not None:
                prefixes = self._store.prefixes(account_id, resolve)
                breach = next(
                    (p for p in prefixes if p > rule.max_exposure + _EPS), None
                )
                if breach is not None:
                    return DecisionRecord(
                        Decision.REJECTED, RejectReason.LIMIT_EXCEEDED,
                        f"更正后账户 {account_id} 历史前缀敞口 {breach} 超过账户上限 "
                        f"{rule.max_exposure}，会改变已上报判定（账户层）",
                        level=LimitLevel.ACCOUNT,
                    )
            group_id = self._groups.group_of(account_id)
            if group_id is not None:
                group_rule = self._groups.group_limit(group_id)
                if group_rule is not None:
                    total = self._compute_group_exposure(group_id, resolve)
                    if total > group_rule.max_exposure + _EPS:
                        return DecisionRecord(
                            Decision.REJECTED, RejectReason.GROUP_LIMIT_EXCEEDED,
                            f"更正后账户组 {group_id} 敞口 {total} 超过组上限 "
                            f"{group_rule.max_exposure}，会改变已上报判定（账户组层）",
                            level=LimitLevel.GROUP,
                        )
            return None

    def _compute_group_exposure(
        self, group_id: str, resolve: DeltaResolver | None
    ) -> float:
        return sum(
            self._store.exposure(member, resolve)
            for member in self._groups.members_of(group_id)
        )

    @staticmethod
    def _validate(event: ExposureEvent) -> DecisionRecord | None:
        if not event.event_id or not event.account_id:
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_EVENT,
                "event_id/account_id 不能为空",
            )
        if event.seq < 0 or not math.isfinite(event.delta):
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_EVENT,
                f"非法事件字段: seq={event.seq}, delta={event.delta}",
            )
        return None
