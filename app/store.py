"""内存事件存储与敞口聚合。

敞口定义为“按 (seq, event_id) 排序、按 event_id 去重后”的事件序列的
确定性函数：逐步累加 delta，任何中间结果为负时截断为 0（并记录截断），
因此重复投递、乱序到达不影响最终结论，也不会出现负数敞口或重复扣减。

更正（冲正）通过 resolve 回调注入：resolve(event_id, raw_delta) 返回
生效金额，返回 None 表示整笔撤销。聚合逻辑保持纯函数，相同输入必得相同结果。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from app.models import ExposureEvent

# resolve(event_id, raw_delta) -> 生效金额；None 表示该事件已被撤销
DeltaResolver = Callable[[str, float], float | None]


def aggregate(
    events: list[ExposureEvent], resolve: DeltaResolver | None = None
) -> tuple[float, list[float]]:
    """按序聚合，返回 (最终敞口, 逐步前缀敞口)。

    前缀列表用于限额判定与更正稳定性校验（撤销事件不改变已走过的前缀，
    因为被撤销的事件在重算视图中直接跳过）。
    """
    total = 0.0
    prefixes: list[float] = []
    for ev in events:
        delta = ev.delta if resolve is None else resolve(ev.event_id, ev.delta)
        if delta is None:
            prefixes.append(total)
            continue
        total = max(0.0, total + delta)
        prefixes.append(total)
    return total, prefixes


class EventStore:
    """按账户保存事件的线程安全内存存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # account_id -> {event_id -> ExposureEvent}
        self._events: dict[str, dict[str, ExposureEvent]] = {}
        # 全局 event_id -> account_id，仅登记已接受（生效）的事件
        self._index: dict[str, str] = {}

    def add(self, event: ExposureEvent) -> bool:
        """登记事件；event_id 已存在时忽略并返回 False（幂等去重）。"""
        with self._lock:
            bucket = self._events.setdefault(event.account_id, {})
            if event.event_id in bucket:
                return False
            bucket[event.event_id] = event
            self._index[event.event_id] = event.account_id
            return True

    def _ordered(self, account_id: str) -> list[ExposureEvent]:
        bucket = self._events.get(account_id, {})
        return sorted(bucket.values(), key=lambda e: (e.seq, e.event_id))

    def exposure(
        self, account_id: str, resolve: DeltaResolver | None = None
    ) -> float:
        """按序去重聚合敞口；逐步累加并在负值处截断为 0。"""
        with self._lock:
            total, _ = aggregate(self._ordered(account_id), resolve)
            return total

    def prefixes(
        self, account_id: str, resolve: DeltaResolver | None = None
    ) -> list[float]:
        """返回账户逐步前缀敞口（确定性，供更正稳定性校验）。"""
        with self._lock:
            _, prefixes = aggregate(self._ordered(account_id), resolve)
            return prefixes

    def account_of(self, event_id: str) -> str | None:
        """原始 event_id 所属账户；不存在（未生效）时返回 None。"""
        with self._lock:
            return self._index.get(event_id)

    def accounts(self) -> list[str]:
        with self._lock:
            return sorted(self._events)

    def events_of(self, account_id: str) -> list[ExposureEvent]:
        """按 (seq, event_id) 排序返回账户已去重事件。"""
        with self._lock:
            return list(self._ordered(account_id))
