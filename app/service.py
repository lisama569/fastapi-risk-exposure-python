"""组合门面：统一事件上报、更正、限额校验与情景重算入口。"""
from __future__ import annotations

from app.limits import LimitChecker
from app.models import (
    CorrectionEvent,
    DecisionRecord,
    ExposureEvent,
    GroupLimitRule,
    LimitRule,
    StressScenario,
)
from app.store import EventStore
from app.stress import StressEngine


class RiskService:
    """持有内存状态，向各入口（HTTP/测试）提供一致语义。"""

    def __init__(self) -> None:
        self.store = EventStore()
        self.limits = LimitChecker(self.store)
        self.stress = StressEngine(self.store, self.limits)

    # ---- 限额与账户组配置 ----

    def set_limit(self, rule: LimitRule) -> None:
        self.limits.set_limit(rule)

    def set_group_limit(self, rule: GroupLimitRule) -> None:
        self.limits.set_group_limit(rule)

    def assign_account(self, account_id: str, group_id: str) -> None:
        self.limits.assign_account(account_id, group_id)

    # ---- 事件与更正 ----

    def submit_event(self, event: ExposureEvent) -> DecisionRecord:
        """幂等上报并在锁内完成两级限额校验与提交。"""
        return self.limits.check_and_commit(event)

    def submit_correction(self, correction: CorrectionEvent) -> DecisionRecord:
        """幂等提交更正（冲正），只读试算逐生效时点限额，通过后才登记。"""
        return self.limits.check_and_commit_correction(correction)

    # ---- 查询 ----

    def exposure(self, account_id: str) -> float:
        return self.store.exposure(account_id)

    def events_of(self, account_id: str):
        return self.store.events_of(account_id)

    def group_exposure(self, group_id: str) -> float:
        return self.limits.group_exposure(group_id)

    def group_usage(self, group_id: str) -> dict:
        return self.limits.group_usage(group_id)

    def stress_snapshot(self, scenario: StressScenario) -> dict:
        return self.stress.snapshot(scenario)
