"""事件模型、限额与情景的类型定义。

金额统一使用 ``Decimal``（字符串传输），避免浮点误差导致结论不一致。
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """敞口变动事件类型。"""

    INCREASE = "INCREASE"  # 增加敞口
    DECREASE = "DECREASE"  # 释放敞口


class RejectReason(str, Enum):
    """可区分的拒绝原因。"""

    NEGATIVE_EXPOSURE = "NEGATIVE_EXPOSURE"  # 事件按序应用后敞口为负
    INVALID_AMOUNT = "INVALID_AMOUNT"  # 金额非法（负数/NaN/非有限值）
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"  # 相同 event_id 载荷冲突
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"  # 超过账户限额
    SCENARIO_LIMIT_EXCEEDED = "SCENARIO_LIMIT_EXCEEDED"  # 压力后超过情景限额
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"  # 缺失基准数据
    SCENARIO_NOT_FOUND = "SCENARIO_NOT_FOUND"  # 情景不存在
    INVALID_SCENARIO = "INVALID_SCENARIO"  # 非法情景参数
    UNCLASSIFIED_ASSET = "UNCLASSIFIED_ASSET"  # 事件资产类别在情景中无折扣
    MISSING_BASELINE = "MISSING_BASELINE"  # 重算时缺失基准敞口数据


class Event(BaseModel):
    """一笔敞口变动事件。

    - ``event_id`` 全局唯一，是幂等去重的键。
    - ``seq`` 为单调逻辑序号；乱序到达时按 (seq, event_id) 归位。
    - ``amount`` 为非负金额，方向由 ``type`` 决定。
    """

    event_id: str
    account_id: str
    seq: int = Field(ge=0)
    type: EventType
    amount: Decimal = Field(max_digits=28, decimal_places=8)
    asset_class: str = "DEFAULT"
    timestamp: str | None = None


class EventAck(BaseModel):
    """事件上报结论（幂等重放结论稳定）。"""

    event_id: str
    account_id: str
    accepted: bool
    duplicate: bool
    exposure_after: Decimal
    state_version: int
    reason: RejectReason | None = None
    detail: str | None = None


class LimitCheck(BaseModel):
    """一次限额提交请求。"""

    request_id: str
    account_id: str
    amount: Decimal = Field(max_digits=28, decimal_places=8)


class LimitResult(BaseModel):
    """限额校验结论。"""

    request_id: str
    account_id: str
    approved: bool
    exposure_before: Decimal
    exposure_after: Decimal
    limit: Decimal
    state_version: int
    reason: RejectReason | None = None
    detail: str | None = None


class ScenarioSpec(BaseModel):
    """压力情景定义：按资产类别的折扣率（0~1）。"""

    scenario_id: str
    haircuts: dict[str, Decimal]
    scenario_limit: Decimal | None = None
    description: str = ""


class ScenarioResult(BaseModel):
    """压力情景重算快照。"""

    scenario_id: str
    account_id: str
    baseline_exposure: Decimal
    stressed_exposure: Decimal
    limit: Decimal | None
    within_limit: bool | None
    applied_events: int
    state_version: int
    reason: RejectReason | None = None
    detail: str | None = None
    per_asset: dict[str, Decimal] = Field(default_factory=dict)
