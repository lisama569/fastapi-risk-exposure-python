"""限额校验与并发提交。

所有“校验 + 提交”在同一把锁内完成：并发请求被串行化，
限额不会被突破；重复事件返回稳定的 DUPLICATE 结论且不改状态。
"""
from __future__ import annotations

import math
import threading

from app.models import Decision, DecisionRecord, ExposureEvent, LimitRule, RejectReason
from app.store import EventStore


class LimitChecker:
    """基于账户敞口上限的校验器。"""

    def __init__(self, store: EventStore) -> None:
        self._lock = threading.RLock()
        self._store = store
        self._rules: dict[str, LimitRule] = {}

    def set_limit(self, rule: LimitRule) -> None:
        if rule.max_exposure < 0 or not math.isfinite(rule.max_exposure):
            raise ValueError(f"非法限额: {rule.max_exposure}")
        with self._lock:
            self._rules[rule.account_id] = rule

    def limit_of(self, account_id: str) -> LimitRule | None:
        with self._lock:
            return self._rules.get(account_id)

    def check_and_commit(self, event: ExposureEvent) -> DecisionRecord:
        """幂等提交事件并校验限额。

        判定顺序（均在锁内，结论可复现）：
        1. 非法事件 -> REJECTED / INVALID_EVENT；
        2. 重复 event_id -> DUPLICATE（不改状态）；
        3. 提交后敞口超限 -> REJECTED / LIMIT_EXCEEDED（不提交）；
        4. 否则 ACCEPTED。
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
            rule = self._rules.get(event.account_id)
            if rule is not None:
                projected = max(0.0, self._store.exposure(event.account_id) + event.delta)
                if projected > rule.max_exposure:
                    return DecisionRecord(
                        Decision.REJECTED, RejectReason.LIMIT_EXCEEDED,
                        f"账户 {event.account_id} 预计敞口 {projected} 超过上限 "
                        f"{rule.max_exposure}",
                    )
            self._store.add(event)
            return DecisionRecord(
                Decision.ACCEPTED, RejectReason.NONE,
                f"事件 {event.event_id} 已接受，账户 {event.account_id} 当前敞口 "
                f"{self._store.exposure(event.account_id)}",
            )

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
