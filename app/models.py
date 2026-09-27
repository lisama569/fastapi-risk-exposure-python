"""数据模型：事件、限额、情景与判定原因（骨架）。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Decision(str, enum.Enum):
    """限额/事件提交的判定结论。"""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class RejectReason(str, enum.Enum):
    """可区分的拒绝原因。"""

    NONE = "NONE"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    GROUP_LIMIT_EXCEEDED = "GROUP_LIMIT_EXCEEDED"
    INVALID_EVENT = "INVALID_EVENT"
    INVALID_CORRECTION = "INVALID_CORRECTION"
    UNKNOWN_TARGET_EVENT = "UNKNOWN_TARGET_EVENT"
    INVALID_SCENARIO = "INVALID_SCENARIO"
    MISSING_BASELINE = "MISSING_BASELINE"


@dataclass(frozen=True)
class ExposureEvent:
    """敞口变动事件。seq 为账户内单调序号，用于乱序重排。"""

    event_id: str
    account_id: str
    seq: int
    delta: float


@dataclass(frozen=True)
class LimitRule:
    """账户级敞口上限。"""

    account_id: str
    max_exposure: float


@dataclass(frozen=True)
class GroupLimitRule:
    """账户组级整体敞口上限（组内所有账户共享）。"""

    group_id: str
    max_exposure: float


@dataclass(frozen=True)
class CorrectionEvent:
    """对已生效上报的更正（冲正）。

    - 按 correction_id 幂等去重；按 target_event_id 引用原始上报；
    - cancel=True 表示整笔撤销（生效 delta 视为 0）；
    - 否则用 new_delta 替换原始事件的 delta；
    - cancel 与 new_delta 互斥，二者都缺省视为非法。
    """

    correction_id: str
    target_event_id: str
    new_delta: float | None = None
    cancel: bool = False


@dataclass(frozen=True)
class StressScenario:
    """压力情景：对账户敞口施加百分比冲击。"""

    name: str
    shocks: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionRecord:
    """一次判定的可解释记录。"""

    decision: Decision
    reason: RejectReason
    detail: str
