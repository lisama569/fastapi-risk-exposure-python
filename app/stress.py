"""压力情景重算与快照（账户层 + 账户组层）。

重算是基准敞口的纯函数：stressed = max(0, base * (1 + shock))。
相同情景 + 相同事件与更正序列 -> 相同快照（含内容哈希，可复现校验）。
非法情景参数或缺失基准数据一律显式拒绝（ValueError），不静默用默认值。

基准敞口一律取“更正后生效视图”：已撤销事件不计、已更正金额按新值计，
因此事件与更正的到达顺序不影响重算结果。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable

from app.groups import GroupRegistry
from app.models import StressScenario
from app.store import EventStore

Resolver = Callable[[str, float], float | None]


class StressEngine:
    """基于当前基准敞口（含更正视图）应用账户层与组合层冲击。"""

    def __init__(
        self,
        store: EventStore,
        groups: GroupRegistry | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._store = store
        self._groups = groups
        self._resolver = resolver

    def _account_base(self, account_id: str) -> float:
        return self._store.exposure(account_id, self._resolver)

    def _group_base(self, group_id: str) -> float:
        assert self._groups is not None
        return sum(
            self._account_base(m) for m in self._groups.members_of(group_id)
        )

    def validate(self, scenario: StressScenario) -> None:
        """校验情景合法性，非法时抛出带原因前缀的 ValueError。"""
        if not scenario.name:
            raise ValueError("INVALID_SCENARIO: 情景名称不能为空")
        if not scenario.shocks and not scenario.group_shocks:
            raise ValueError("INVALID_SCENARIO: 情景至少需要一个冲击项")
        known = set(self._store.accounts())
        for account_id, shock in scenario.shocks.items():
            if not isinstance(shock, (int, float)) or not math.isfinite(shock):
                raise ValueError(
                    f"INVALID_SCENARIO: 账户 {account_id} 冲击值非法: {shock}"
                )
            if shock < -1.0:
                raise ValueError(
                    f"INVALID_SCENARIO: 账户 {account_id} 冲击 {shock} 低于 -100%"
                )
            if account_id not in known:
                raise ValueError(
                    f"MISSING_BASELINE: 账户 {account_id} 无基准敞口数据"
                )
        if self._groups is None and scenario.group_shocks:
            raise ValueError(
                "INVALID_SCENARIO: 当前未启用账户组，不能施加组合冲击"
            )
        if self._groups is not None:
            known_groups = set(self._groups.groups())
            for group_id, shock in scenario.group_shocks.items():
                if not isinstance(shock, (int, float)) or not math.isfinite(shock):
                    raise ValueError(
                        f"INVALID_SCENARIO: 账户组 {group_id} 冲击值非法: {shock}"
                    )
                if shock < -1.0:
                    raise ValueError(
                        f"INVALID_SCENARIO: 账户组 {group_id} 冲击 {shock} 低于 -100%"
                    )
                if group_id not in known_groups:
                    raise ValueError(
                        f"MISSING_BASELINE: 账户组 {group_id} 无基准数据"
                    )

    def recalculate(self, scenario: StressScenario) -> dict[str, float]:
        """返回 {account_id: 冲击后敞口}，键排序保证确定性。"""
        self.validate(scenario)
        result: dict[str, float] = {}
        for account_id in self._store.accounts():
            base = self._account_base(account_id)
            shock = scenario.shocks.get(account_id, 0.0)
            result[account_id] = max(0.0, base * (1.0 + shock))
        return dict(sorted(result.items()))

    def recalculate_groups(self, scenario: StressScenario) -> dict[str, float]:
        """返回 {group_id: 组合层冲击后敞口}，键排序保证确定性。"""
        self.validate(scenario)
        if self._groups is None:
            return {}
        result: dict[str, float] = {}
        for group_id in sorted(scenario.group_shocks):
            base = self._group_base(group_id)
            shock = scenario.group_shocks[group_id]
            result[group_id] = max(0.0, base * (1.0 + shock))
        return dict(sorted(result.items()))

    def snapshot(self, scenario: StressScenario) -> dict:
        """确定性快照：账户层与组合层基准、冲击后敞口与内容哈希。

        原有字段（scenario/shocks/base_exposure/stressed_exposure/hash）保持
        不变；组合层以独立字段追加，旧调用方不受影响。
        """
        stressed = self.recalculate(scenario)
        base = {a: self._account_base(a) for a in stressed}
        payload = {
            "scenario": scenario.name,
            "shocks": dict(sorted(scenario.shocks.items())),
            "base_exposure": base,
            "stressed_exposure": stressed,
        }
        if scenario.group_shocks:
            group_base = {
                g: self._group_base(g) for g in sorted(scenario.group_shocks)
            }
            payload["group_shocks"] = dict(sorted(scenario.group_shocks.items()))
            payload["group_base_exposure"] = group_base
            payload["group_stressed_exposure"] = self.recalculate_groups(scenario)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {**payload, "hash": digest}
