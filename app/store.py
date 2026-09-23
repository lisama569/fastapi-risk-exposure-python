"""线程安全的本地内存状态：账户基准、已处理事件、情景定义。

并发模型：
- 每个账户一把可重入锁 ``RLock``，事件上报与限额提交都在账户锁内完成
  “读取-判定-写入”，保证并发下限额不会被突破。
- 事件台账以全局 ``event_id`` 为键，保证幂等；同一 id 重复投递只接受第一次
  所见载荷，载荷冲突明确拒绝。
- 事件按 (seq, event_id) 保序存放，乱序到达后仍可按序重放得到收敛敞口。
"""
from __future__ import annotations

import threading
from bisect import bisect_left
from decimal import Decimal

from app.models import Event, ScenarioSpec

ZERO = Decimal("0")


class InMemoryStore:
    """以账户为粒度加锁的内存状态。"""

    def __init__(self) -> None:
        self._global_lock = threading.RLock()
        self._account_locks: dict[str, threading.RLock] = {}
        self._accounts: set[str] = set()
        self._limits: dict[str, Decimal] = {}
        self._exposure: dict[str, Decimal] = {}
        self._per_asset: dict[str, dict[str, Decimal]] = {}
        self._version: dict[str, int] = {}
        # 全局幂等台账：event_id -> Event（包含到达时尚不可入账的事件）
        self._events: dict[str, Event] = {}
        # 每账户按 (seq, event_id) 保序的全部已见事件
        self._seen: dict[str, list[Event]] = {}
        self._scenarios: dict[str, ScenarioSpec] = {}

    # ---- 锁 ----------------------------------------------------------------

    def account_lock(self, account_id: str) -> threading.RLock:
        """获取（或创建）账户级锁。"""
        with self._global_lock:
            lock = self._account_locks.get(account_id)
            if lock is None:
                lock = threading.RLock()
                self._account_locks[account_id] = lock
            return lock

    # ---- 账户基准 -----------------------------------------------------------

    def ensure_account(self, account_id: str, limit: Decimal | None = None) -> None:
        """创建账户基准数据；已存在时可顺带补充限额。"""
        with self._global_lock:
            if account_id not in self._accounts:
                self._accounts.add(account_id)
                self._exposure[account_id] = ZERO
                self._per_asset[account_id] = {}
                self._version[account_id] = 0
                self._seen[account_id] = []
            if limit is not None:
                self._limits[account_id] = limit

    def has_account(self, account_id: str) -> bool:
        with self._global_lock:
            return account_id in self._accounts

    def get_exposure(self, account_id: str) -> Decimal:
        return self._exposure.get(account_id, ZERO)

    def get_per_asset(self, account_id: str) -> dict[str, Decimal]:
        """返回按资产类别拆分敞口的副本。"""
        return dict(self._per_asset.get(account_id, {}))

    def get_limit(self, account_id: str) -> Decimal | None:
        return self._limits.get(account_id)

    def set_limit(self, account_id: str, limit: Decimal) -> None:
        self._limits[account_id] = limit

    def get_version(self, account_id: str) -> int:
        return self._version.get(account_id, 0)

    def bump_version(self, account_id: str) -> None:
        self._version[account_id] = self._version.get(account_id, 0) + 1

    # ---- 事件台账 -----------------------------------------------------------

    def get_event(self, event_id: str) -> Event | None:
        return self._events.get(event_id)

    def has_event(self, event_id: str) -> bool:
        return event_id in self._events

    def seen_events(self, account_id: str) -> list[Event]:
        """按 (seq, event_id) 顺序返回全部已见事件（含暂未生效者）。"""
        return list(self._seen.get(account_id, []))

    @staticmethod
    def _sort_key(event: Event) -> tuple[int, str]:
        return (event.seq, event.event_id)

    def insert_event(self, event: Event) -> None:
        """将事件登记入全局已见台账，并按序插入账户事件序列。"""
        self._events[event.event_id] = event
        ordered = self._seen.setdefault(event.account_id, [])
        keys = [self._sort_key(e) for e in ordered]
        pos = bisect_left(keys, self._sort_key(event))
        ordered.insert(pos, event)

    # ---- 敞口变动 -----------------------------------------------------------

    def apply_delta(self, account_id: str, signed_delta: Decimal, asset_class: str) -> None:
        """在账户锁内把带符号增量叠加到总敞口与资产类别敞口上。"""
        total = self._exposure.get(account_id, ZERO) + signed_delta
        self._exposure[account_id] = total
        bucket = self._per_asset.setdefault(account_id, {})
        bucket[asset_class] = bucket.get(asset_class, ZERO) + signed_delta
        self.bump_version(account_id)

    def replace_exposure(
        self,
        account_id: str,
        total: Decimal,
        per_asset: dict[str, Decimal],
    ) -> bool:
        """重放后以收敛结果替换敞口快照；仅在实际变化时升版本，返回是否变化。"""
        changed = (
            self._exposure.get(account_id, ZERO) != total
            or self._per_asset.get(account_id, {}) != per_asset
        )
        if changed:
            self._exposure[account_id] = total
            self._per_asset[account_id] = dict(per_asset)
            self.bump_version(account_id)
        return changed

    # ---- 情景 ---------------------------------------------------------------

    def put_scenario(self, spec: ScenarioSpec) -> None:
        self._scenarios[spec.scenario_id] = spec

    def get_scenario(self, scenario_id: str) -> ScenarioSpec | None:
        return self._scenarios.get(scenario_id)
