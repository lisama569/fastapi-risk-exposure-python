"""压力情景重算与快照。

重算是基准敞口的纯函数：stressed = base * (1 + shock)。
相同情景 + 相同事件/更正序列 -> 相同快照（含内容哈希，可复现校验）。
非法情景参数或缺失基准数据一律显式拒绝（ValueError），不静默用默认值。

快照同时给出账户层与账户组层结果：账户组敞口 = 组内各账户敞口之和，
冲击在账户层施加后聚合到组层，因此组层结果同样可复现。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import TYPE_CHECKING

from app.models import StressScenario
from app.store import EventStore

if TYPE_CHECKING:
    from app.limits import LimitChecker


class StressEngine:
    """基于当前基准敞口应用场景冲击。"""

    def __init__(self, store: EventStore, limits: LimitChecker | None = None) -> None:
        self._store = store
        self._limits = limits

    def validate(self, scenario: StressScenario) -> None:
        """校验情景合法性，非法时抛出带原因的 ValueError。"""
        if not scenario.name:
            raise ValueError("INVALID_SCENARIO: 情景名称不能为空")
        if not scenario.shocks:
            raise ValueError("INVALID_SCENARIO: 情景至少需要一个冲击项")
        known = set(self._store.accounts())
        for account_id, shock in scenario.shocks.items():
            if not isinstance(shock, (int, float)) or not math.isfinite(shock):
                raise ValueError(f"INVALID_SCENARIO: 账户 {account_id} 冲击值非法: {shock}")
            if shock < -1.0:
                raise ValueError(
                    f"INVALID_SCENARIO: 账户 {account_id} 冲击 {shock} 低于 -100%"
                )
            if account_id not in known:
                raise ValueError(
                    f"MISSING_BASELINE: 账户 {account_id} 无基准敞口数据"
                )

    def recalculate(self, scenario: StressScenario) -> dict[str, float]:
        """返回 {account_id: 冲击后敞口}，键排序保证确定性。"""
        self.validate(scenario)
        result: dict[str, float] = {}
        for account_id in self._store.accounts():
            base = self._store.exposure(account_id)
            shock = scenario.shocks.get(account_id, 0.0)
            result[account_id] = max(0.0, base * (1.0 + shock))
        return dict(sorted(result.items()))

    def _group_rollups(self, per_account: dict[str, float]) -> dict[str, float]:
        """把账户层数值聚合到账户组层（组 = 成员之和，键排序）。"""
        if self._limits is None:
            return {}
        groups: dict[str, float] = {}
        for account_id, value in per_account.items():
            group_id = self._limits.group_of(account_id)
            if group_id is not None:
                groups[group_id] = groups.get(group_id, 0.0) + value
        return dict(sorted(groups.items()))

    def snapshot(self, scenario: StressScenario) -> dict:
        """确定性快照：账户层与账户组层的基准/冲击后敞口及内容哈希。"""
        stressed = self.recalculate(scenario)
        base = {a: self._store.exposure(a) for a in stressed}
        payload = {
            "scenario": scenario.name,
            "shocks": dict(sorted(scenario.shocks.items())),
            "base_exposure": base,
            "stressed_exposure": stressed,
            "group_base_exposure": self._group_rollups(base),
            "group_stressed_exposure": self._group_rollups(stressed),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {**payload, "hash": digest}
