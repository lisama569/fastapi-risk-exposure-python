"""组合门面：统一事件上报、更正、两层限额校验与情景重算入口。"""
from __future__ import annotations

import threading

from app.corrections import CorrectionManager
from app.groups import GroupRegistry
from app.limits import LimitChecker
from app.models import (
    DecisionRecord,
    EventCorrection,
    ExposureEvent,
    GroupMembership,
    GroupRule,
    LimitRule,
    StressScenario,
)
from app.store import EventStore
from app.stress import StressEngine


class RiskService:
    """持有内存状态，向各入口（HTTP/测试）提供一致语义。"""

    def __init__(self) -> None:
        self.store = EventStore()
        self.groups = GroupRegistry()
        # 事件上报与更正确认共享同一把锁：并发下两层额度都不被突破
        self._lock = threading.RLock()
        self.limits = LimitChecker(self.store, self.groups, self._lock)
        self.corrections = CorrectionManager(
            self.store, self.limits.validate_correction_view, self._lock
        )
        self.limits.bind_resolver(self.corrections.resolve)
        self.stress = StressEngine(self.store, self.groups, self.corrections.resolve)

    def set_limit(self, rule: LimitRule) -> None:
        self.limits.set_limit(rule)

    def set_group_limit(self, rule: GroupRule) -> None:
        self.groups.set_group_limit(rule)

    def assign_account(self, membership: GroupMembership) -> None:
        self.groups.assign(membership)

    def submit_event(self, event: ExposureEvent) -> DecisionRecord:
        """幂等上报并在锁内完成账户层、账户组层限额校验与提交。"""
        return self.limits.check_and_commit(event)

    def submit_correction(self, correction: EventCorrection) -> DecisionRecord:
        """提交更正（冲正）：识别原始上报、幂等、限额稳定性校验后生效。"""
        return self.corrections.submit(correction)

    def exposure(self, account_id: str) -> float:
        return self.store.exposure(account_id, self.corrections.resolve)

    def group_exposure(self, group_id: str) -> float:
        return self.limits.group_exposure(group_id)

    def group_occupancy(self, group_id: str) -> dict:
        """账户组额度占用明细：各成员占用、合计、上限与剩余额度。"""
        rule = self.groups.group_limit(group_id)
        members = self.groups.members_of(group_id)
        per_account = {
            m: self.store.exposure(m, self.corrections.resolve) for m in members
        }
        total = sum(per_account.values())
        limit = rule.max_exposure if rule is not None else None
        return {
            "group_id": group_id,
            "max_exposure": limit,
            "per_account": dict(sorted(per_account.items())),
            "used": total,
            "remaining": (limit - total) if limit is not None else None,
        }

    def events_of(self, account_id: str):
        return self.store.events_of(account_id)

    def stress_snapshot(self, scenario: StressScenario) -> dict:
        return self.stress.snapshot(scenario)
