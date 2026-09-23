"""内存事件存储与敞口聚合。

敞口定义为“按 (seq, event_id) 排序、按 event_id 去重后”的事件序列的
确定性函数：逐步累加 delta，任何中间结果为负时截断为 0（并记录截断），
因此重复投递、乱序到达不影响最终结论，也不会出现负数敞口或重复扣减。
"""
from __future__ import annotations

import threading

from app.models import ExposureEvent


class EventStore:
    """按账户保存事件的线程安全内存存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # account_id -> {event_id -> ExposureEvent}
        self._events: dict[str, dict[str, ExposureEvent]] = {}

    def add(self, event: ExposureEvent) -> bool:
        """登记事件；event_id 已存在时忽略并返回 False（幂等去重）。"""
        with self._lock:
            bucket = self._events.setdefault(event.account_id, {})
            if event.event_id in bucket:
                return False
            bucket[event.event_id] = event
            return True

    def _ordered(self, account_id: str) -> list[ExposureEvent]:
        bucket = self._events.get(account_id, {})
        return sorted(bucket.values(), key=lambda e: (e.seq, e.event_id))

    def exposure(self, account_id: str) -> float:
        """按序去重聚合敞口；逐步累加并在负值处截断为 0。"""
        with self._lock:
            total = 0.0
            for ev in self._ordered(account_id):
                total = max(0.0, total + ev.delta)
            return total

    def accounts(self) -> list[str]:
        with self._lock:
            return sorted(self._events)

    def events_of(self, account_id: str) -> list[ExposureEvent]:
        """按 (seq, event_id) 排序返回账户已去重事件。"""
        with self._lock:
            return list(self._ordered(account_id))
