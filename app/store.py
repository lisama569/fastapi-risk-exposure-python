"""内存事件存储与敞口聚合。

敞口定义为“按 (seq, event_id) 排序、按 event_id 去重后”的事件序列的
确定性函数：逐步累加“生效 delta”，任何中间结果为负时截断为 0（并记录截断），
因此重复投递、乱序到达不影响最终结论，也不会出现负数敞口或重复扣减。

生效 delta：若某事件存在已登记的更正（按 target_event_id 索引），
撤销类更正视为 0，改值类更正用 new_delta 替换原 delta。
更正按 correction_id 幂等去重，且每个目标事件至多被更正一次，
因此最终敞口是 {事件集合, 更正集合} 的确定性函数，与到达顺序无关。
"""
from __future__ import annotations

import threading

from app.models import CorrectionEvent, ExposureEvent


class EventStore:
    """按账户保存事件、按目标索引更正的线程安全内存存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # account_id -> {event_id -> ExposureEvent}
        self._events: dict[str, dict[str, ExposureEvent]] = {}
        # correction_id -> CorrectionEvent
        self._corrections: dict[str, CorrectionEvent] = {}
        # target_event_id -> correction_id（每个目标至多一条生效更正）
        self._correction_by_target: dict[str, str] = {}

    def add(self, event: ExposureEvent) -> bool:
        """登记事件；event_id 已存在时忽略并返回 False（幂等去重）。"""
        with self._lock:
            bucket = self._events.setdefault(event.account_id, {})
            if event.event_id in bucket:
                return False
            bucket[event.event_id] = event
            return True

    def find_event(self, event_id: str) -> ExposureEvent | None:
        """跨账户查找原始事件（更正按 target_event_id 定位用）。"""
        with self._lock:
            for bucket in self._events.values():
                if event_id in bucket:
                    return bucket[event_id]
            return None

    def correction_of(self, correction_id: str) -> CorrectionEvent | None:
        with self._lock:
            return self._corrections.get(correction_id)

    def correction_for_target(self, target_event_id: str) -> CorrectionEvent | None:
        with self._lock:
            cid = self._correction_by_target.get(target_event_id)
            return self._corrections.get(cid) if cid else None

    def add_correction(self, correction: CorrectionEvent) -> bool:
        """登记更正；correction_id 已存在时忽略并返回 False（幂等去重）。"""
        with self._lock:
            if correction.correction_id in self._corrections:
                return False
            self._corrections[correction.correction_id] = correction
            self._correction_by_target[correction.target_event_id] = (
                correction.correction_id
            )
            return True

    def remove_correction(self, correction_id: str) -> None:
        """撤销已登记更正（仅供提交入口在限额试算失败时回滚）。"""
        with self._lock:
            corr = self._corrections.pop(correction_id, None)
            if corr is not None:
                self._correction_by_target.pop(corr.target_event_id, None)

    def _effective_delta(self, event: ExposureEvent) -> float:
        corr = self.correction_for_target(event.event_id)
        if corr is None:
            return event.delta
        if corr.cancel:
            return 0.0
        assert corr.new_delta is not None  # 合法性由提交入口保证
        return corr.new_delta

    def _ordered(self, account_id: str) -> list[ExposureEvent]:
        bucket = self._events.get(account_id, {})
        return sorted(bucket.values(), key=lambda e: (e.seq, e.event_id))

    def exposure(self, account_id: str) -> float:
        """按序去重聚合敞口（应用更正后的生效 delta），负值处截断为 0。"""
        with self._lock:
            return self._exposure_locked(account_id)

    def _exposure_locked(self, account_id: str) -> float:
        total = 0.0
        for ev in self._ordered(account_id):
            total = max(0.0, total + self._effective_delta(ev))
        return total

    def effective_deltas(self, account_id: str) -> list[float]:
        """返回账户按序去重后每笔事件的生效 delta（更正后口径）。"""
        with self._lock:
            return [self._effective_delta(ev) for ev in self._ordered(account_id)]

    def effective_path(self, account_id: str) -> list[float]:
        """返回账户按序去重后每个生效时点的敞口（含 0 截断）。

        更正会改写历史事件的生效 delta，因此不能只看最终敞口：
        限额试算必须逐时点检查更正后的整条轨迹，任何一个时点超限都算放不下。
        """
        with self._lock:
            path: list[float] = []
            total = 0.0
            for ev in self._ordered(account_id):
                total = max(0.0, total + self._effective_delta(ev))
                path.append(total)
            return path

    def accounts(self) -> list[str]:
        with self._lock:
            return sorted(self._events)

    def events_of(self, account_id: str) -> list[ExposureEvent]:
        """按 (seq, event_id) 排序返回账户已去重事件。"""
        with self._lock:
            return list(self._ordered(account_id))
