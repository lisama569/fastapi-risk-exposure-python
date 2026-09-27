"""事件更正（冲正）：识别、幂等生效与确定性重算。

语义：
- 更正通过 original_event_id 引用一笔**已生效**的原始上报；引用不存在
  （或原始上报此前被拒绝）的更正一律拒绝：ORIGINAL_NOT_FOUND，可重试；
- correction_id 是更正自身的幂等键，同一份更正重复提交返回 DUPLICATE，
  不重复生效，也绝不当成一笔新的敞口变动；
- 一笔原始上报至多被一份更正生效：第二份（不同 correction_id）更正
  拒绝：CORRECTION_CONFLICT；
- 字段非法（空 id、new_delta 非有限、同时给金额又撤销等）拒绝：
  INVALID_CORRECTION；
- 生效后必须保持历史判定不再改变：注入的 validator 对“更正后视图”
  重放账户/账户组两层限额，放不下则拒绝（LIMIT_EXCEEDED /
  GROUP_LIMIT_EXCEEDED），不允许出现结论不确定。

重算视图是原始事件集合 + 更正集合的纯函数：无论更正先到、后到还是与
原始上报交错到达（ORIGINAL_NOT_FOUND 可在原始上报到达后重试生效），
收敛后的口径一致。
"""
from __future__ import annotations

import math
import threading
from collections.abc import Callable

from app.models import (
    Decision,
    DecisionRecord,
    EventCorrection,
    RejectReason,
)
from app.store import DeltaResolver, EventStore

# 校验“更正后视图”是否会让既有结论失稳；返回 None 表示通过
Validator = Callable[[DeltaResolver, str], DecisionRecord | None]


class CorrectionManager:
    """管理更正的登记与生效。"""

    def __init__(
        self,
        store: EventStore,
        validator: Validator,
        lock: threading.RLock | None = None,
    ) -> None:
        self._store = store
        self._validator = validator
        self._lock = lock if lock is not None else threading.RLock()
        # correction_id -> EventCorrection
        self._items: dict[str, EventCorrection] = {}
        # original_event_id -> correction_id
        self._by_original: dict[str, str] = {}

    def submit(self, correction: EventCorrection) -> DecisionRecord:
        with self._lock:
            invalid = self._validate_fields(correction)
            if invalid is not None:
                return invalid
            if correction.correction_id in self._items:
                return DecisionRecord(
                    Decision.DUPLICATE, RejectReason.NONE,
                    f"更正 {correction.correction_id} 已生效，幂等忽略",
                )
            account_id = self._store.account_of(correction.original_event_id)
            if account_id is None:
                return DecisionRecord(
                    Decision.REJECTED, RejectReason.ORIGINAL_NOT_FOUND,
                    f"更正引用的原始上报 {correction.original_event_id} 不存在"
                    f"或未生效，可在原始上报到达后重试",
                )
            existing = self._by_original.get(correction.original_event_id)
            if existing is not None:
                return DecisionRecord(
                    Decision.REJECTED, RejectReason.CORRECTION_CONFLICT,
                    f"原始上报 {correction.original_event_id} 已被更正 "
                    f"{existing} 生效，不能再次更正",
                )

            # 构造“假设该更正生效”的临时视图，校验历史结论稳定性
            def trial(event_id: str, raw: float) -> float | None:
                if event_id == correction.original_event_id:
                    return None if correction.cancel else correction.new_delta
                return self.resolve(event_id, raw)

            breach = self._validator(trial, account_id)
            if breach is not None:
                return breach

            self._items[correction.correction_id] = correction
            self._by_original[correction.original_event_id] = correction.correction_id
            return DecisionRecord(
                Decision.ACCEPTED, RejectReason.NONE,
                f"更正 {correction.correction_id} 已生效，原始上报 "
                f"{correction.original_event_id} "
                f"{'整笔撤销' if correction.cancel else f'金额更正为 {correction.new_delta}'}",
            )

    def resolve(self, event_id: str, raw_delta: float) -> float | None:
        """生效金额视图：返回更正后的 delta；被撤销返回 None；未更正返回原值。"""
        cid = self._by_original.get(event_id)
        if cid is None:
            return raw_delta
        corr = self._items[cid]
        return None if corr.cancel else corr.new_delta

    def is_cancelled(self, event_id: str) -> bool:
        cid = self._by_original.get(event_id)
        if cid is None:
            return False
        return self._items[cid].cancel

    @staticmethod
    def _validate_fields(c: EventCorrection) -> DecisionRecord | None:
        if not c.correction_id or not c.original_event_id:
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                "correction_id/original_event_id 不能为空",
            )
        if c.cancel and c.new_delta is not None:
            return DecisionRecord(
                Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                "撤销更正不能同时提供 new_delta",
            )
        if not c.cancel:
            if c.new_delta is None or not math.isfinite(c.new_delta):
                return DecisionRecord(
                    Decision.REJECTED, RejectReason.INVALID_CORRECTION,
                    f"更正金额非法: new_delta={c.new_delta}",
                )
        return None
