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
    INVALID_SCENARIO = "INVALID_SCENARIO"
    MISSING_BASELINE = "MISSING_BASELINE"
    # 更正（冲正）相关原因
    ORIGINAL_NOT_FOUND = "ORIGINAL_NOT_FOUND"
    INVALID_CORRECTION = "INVALID_CORRECTION"
    CORRECTION_CONFLICT = "CORRECTION_CONFLICT"


class LimitLevel(str, enum.Enum):
    """拒绝发生的限额层级。"""

    NONE = "NONE"
    ACCOUNT = "ACCOUNT"
    GROUP = "GROUP"


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
class GroupRule:
    """账户组（组合）级共享敞口上限。

    一个账户至多属于一个账户组；组内成员敞口之和不得超过整体上限。
    """

    group_id: str
    max_exposure: float


@dataclass(frozen=True)
class GroupMembership:
    """账户 -> 账户组 的归属关系。"""

    account_id: str
    group_id: str


@dataclass(frozen=True)
class EventCorrection:
    """对一笔已生效原始上报的更正（冲正）。

    - correction_id：更正自身的幂等标识，重复提交不重复生效；
    - original_event_id：被更正的原始上报 event_id；
    - new_delta / cancel：二选一，给出更正后的金额或整笔撤销。
    """

    correction_id: str
    original_event_id: str
    new_delta: float | None = None
    cancel: bool = False


@dataclass(frozen=True)
class StressScenario:
    """压力情景：对账户/账户组敞口施加百分比冲击。"""

    name: str
    shocks: dict[str, float] = field(default_factory=dict)
    group_shocks: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionRecord:
    """一次判定的可解释记录。"""

    decision: Decision
    reason: RejectReason
    detail: str
    level: LimitLevel = LimitLevel.NONE
