"""FastAPI HTTP 入口。

所有入口都复用同一套内存领域服务，因此 HTTP 上报与直接调用领域服务的
判定结论完全一致；业务拒绝（限额突破、负敞口、缺基准、非法情景等）
以 4xx 返回并携带可区分的 ``reason``，不使用 500 静默吞掉。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.models import (
    Event,
    EventAck,
    LimitCheck,
    LimitResult,
    RejectReason,
    ScenarioResult,
    ScenarioSpec,
)
from app.scenarios import ScenarioEngine, ScenarioValidationError
from app.service import RiskService
from app.store import InMemoryStore

store = InMemoryStore()
risk_service = RiskService(store)
scenario_engine = ScenarioEngine(store)

app = FastAPI(title="risk-exposure", version="0.1.0")


class AccountCreate(BaseModel):
    limit: Decimal | None = None


class SimpleAck(BaseModel):
    account_id: str
    limit: Decimal | None
    state_version: int


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/accounts/{account_id}", response_model=SimpleAck)
def create_account(account_id: str, body: AccountCreate | None = None) -> SimpleAck:
    limit = body.limit if body is not None else None
    if limit is not None and (not limit.is_finite() or limit < Decimal("0")):
        raise HTTPException(
            status_code=422,
            detail={"reason": RejectReason.INVALID_AMOUNT.value,
                    "detail": f"限额必须为非负有限值，收到 {limit}"},
        )
    store.ensure_account(account_id, limit)
    return SimpleAck(account_id=account_id, limit=limit,
                     state_version=store.get_version(account_id))


@app.post("/events", response_model=EventAck)
def report_event(event: Event) -> EventAck:
    return risk_service.report_event(event)


@app.post("/limits/check", response_model=LimitResult)
def check_limit(req: LimitCheck) -> LimitResult:
    return risk_service.check_limit(req)


@app.post("/scenarios", status_code=201)
def put_scenario(spec: ScenarioSpec) -> dict:
    try:
        scenario_engine.validate_spec(spec)
    except (ScenarioValidationError, InvalidOperation) as exc:
        raise HTTPException(
            status_code=422,
            detail={"reason": RejectReason.INVALID_SCENARIO.value, "detail": str(exc)},
        ) from exc
    store.put_scenario(spec)
    return {"scenario_id": spec.scenario_id, "registered": True}


@app.get("/scenarios/{scenario_id}/accounts/{account_id}", response_model=ScenarioResult)
def evaluate_scenario(scenario_id: str, account_id: str) -> ScenarioResult:
    result = scenario_engine.evaluate(account_id, scenario_id)
    # 缺失基准/未知情景/资产无折扣属于明确的客户端错误
    if result.reason in (
        RejectReason.SCENARIO_NOT_FOUND,
        RejectReason.MISSING_BASELINE,
        RejectReason.UNCLASSIFIED_ASSET,
    ):
        raise HTTPException(status_code=422, detail=result.model_dump(mode="json"))
    return result
