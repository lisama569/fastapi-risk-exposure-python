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
    INVALID_EVENT = "INVALID_EVENT"
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
