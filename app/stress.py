"""压力情景重算与快照。

重算是基准敞口的纯函数：stressed = base * (1 + shock)。
相同情景 + 相同事件序列 -> 相同快照（含内容哈希，可复现校验）。
非法情景参数或缺失基准数据一律显式拒绝（ValueError），不静默用默认值。
"""
from __future__ import annotations

import hashlib
import json
import math

from app.models import StressScenario
from app.store import EventStore


class StressEngine:
    """基于当前基准敞口应用场景冲击。"""

    def __init__(self, store: EventStore) -> None:
        self._store = store

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

    def snapshot(self, scenario: StressScenario) -> dict:
        """确定性快照：基准、冲击后敞口与内容哈希。"""
        stressed = self.recalculate(scenario)
        base = {a: self._store.exposure(a) for a in stressed}
        payload = {
            "scenario": scenario.name,
            "shocks": dict(sorted(scenario.shocks.items())),
            "base_exposure": base,
            "stressed_exposure": stressed,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {**payload, "hash": digest}
