"""内存事件存储与敞口聚合。

敞口定义为“按 (seq, event_id) 排序、按 event_id 去重后”的事件序列的
确定性函数：逐步累加“生效 delta”，任何中间结果为负时截断为 0（并记录截断），
因此重复投递、乱序到达不影响最终结论，也不会出现负数敞口或重复扣减。

生效 delta：若某事件存在已登记的更正（按 target_event_id 索引），
撤销类更正视为 0，改值类更正用 new_delta 替换原 delta。
更正按 correction_id 幂等去重，且每个目标事件至多被更正一次，
因此最终敞口是 {事件集合, 更正集合} 的确定性函数，与到达顺序无关。

更正限额校验需要检查“每一个生效时点”而非只看最新总额：exposure 只给出
序列终点，effective_path / group_effective_path 额外给出逐步累加的中间
敞口（含终点）。两者都支持 overrides 试算参数
（target_event_id -> 生效 delta），试算只读、绝不改动已登记状态，
因此被拒更正不存在任何“先生效、后越限”的中间窗口，也无需回滚。
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

    def _resolved_delta(
        self,
        event: ExposureEvent,
        overrides: dict[str, float] | None = None,
    ) -> float:
        """事件的生效 delta；overrides（试算更正）优先于已登记更正。"""
        if overrides is not None and event.event_id in overrides:
            return overrides[event.event_id]
        corr = self.correction_for_target(event.event_id)
        if corr is None:
            return event.delta
        if corr.cancel:
            return 0.0
        assert corr.new_delta is not None  # 合法性由提交入口保证
        return corr.new_delta

    def effective_path(
        self,
        account_id: str,
        overrides: dict[str, float] | None = None,
    ) -> list[tuple[ExposureEvent, float]]:
        """逐生效时点返回 (事件, 截至该事件的累计敞口)，负值处截断为 0。

        末项的累计敞口即 exposure(account_id)；中间项即各历史生效时点的
        敞口，供更正限额校验逐点检查。overrides 仅用于只读试算。
        """
        with self._lock:
            path: list[tuple[ExposureEvent, float]] = []
            total = 0.0
            for ev in self._ordered(account_id):
                total = max(0.0, total + self._resolved_delta(ev, overrides))
                path.append((ev, total))
            return path

    def group_effective_path(
        self,
        account_ids: list[str],
        overrides_by_account: dict[str, dict[str, float]] | None = None,
    ) -> list[tuple[str, ExposureEvent, float]]:
        """账户组在全局生效时序下的逐点 (账户, 事件, 组累计敞口)。

        把组内各账户的有序事件流按 (seq, account_id, event_id) 确定性合并；
        每步在事件所属账户上累加其生效 delta（该账户负值截断为 0），
        组累计 = 当时各账户累计之和。末项即组当前敞口，中间项即各历史
        生效时点的组敞口。试算覆盖按账户分桶传入，只读不改状态。
        """
        with self._lock:
            merged: list[tuple[str, ExposureEvent]] = []
            for account_id in account_ids:
                for ev in self._ordered(account_id):
                    merged.append((account_id, ev))
            merged.sort(key=lambda x: (x[1].seq, x[0], x[1].event_id))
            running: dict[str, float] = {a: 0.0 for a in account_ids}
            path: list[tuple[str, ExposureEvent, float]] = []
            for account_id, ev in merged:
                overrides = (
                    overrides_by_account.get(account_id)
                    if overrides_by_account is not None else None
                )
                running[account_id] = max(
                    0.0,
                    running[account_id] + self._resolved_delta(ev, overrides),
                )
                path.append((account_id, ev, sum(running.values())))
            return path

    def _effective_delta(self, event: ExposureEvent) -> float:
        return self._resolved_delta(event)

    def _ordered(self, account_id: str) -> list[ExposureEvent]:
        bucket = self._events.get(account_id, {})
        return sorted(bucket.values(), key=lambda e: (e.seq, e.event_id))

    def exposure(self, account_id: str) -> float:
        """按序去重聚合敞口（应用更正后的生效 delta），负值处截断为 0。"""
        with self._lock:
            total = 0.0
            for ev in self._ordered(account_id):
                total = max(0.0, total + self._effective_delta(ev))
            return total

    def accounts(self) -> list[str]:
        with self._lock:
            return sorted(self._events)

    def events_of(self, account_id: str) -> list[ExposureEvent]:
        """按 (seq, event_id) 排序返回账户已去重事件。"""
        with self._lock:
            return list(self._ordered(account_id))
