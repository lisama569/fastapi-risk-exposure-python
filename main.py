"""FastAPI 入口：事件上报、更正、两层限额、组合占用与情景重算的 HTTP 适配层。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.models import (
    Decision,
    EventCorrection,
    ExposureEvent,
    GroupMembership,
    GroupRule,
    LimitRule,
    StressScenario,
)
from app.service import RiskService

app = FastAPI(title="risk-exposure")
service = RiskService()


class EventIn(BaseModel):
    event_id: str
    account_id: str
    seq: int
    delta: float


class LimitIn(BaseModel):
    max_exposure: float


class MembershipIn(BaseModel):
    group_id: str


class CorrectionIn(BaseModel):
    correction_id: str
    original_event_id: str
    new_delta: float | None = None
    cancel: bool = False


class ScenarioIn(BaseModel):
    name: str
    shocks: dict[str, float] = {}
    group_shocks: dict[str, float] = {}


def _decision_response(record):
    status = 200 if record.decision is not Decision.REJECTED else 409
    return JSONResponse(
        status_code=status,
        content={
            "decision": record.decision.value,
            "reason": record.reason.value,
            "level": record.level.value,
            "detail": record.detail,
        },
    )


@app.post("/events")
def submit_event(body: EventIn):
    return _decision_response(
        service.submit_event(ExposureEvent(**body.model_dump()))
    )


@app.post("/corrections")
def submit_correction(body: CorrectionIn):
    return _decision_response(
        service.submit_correction(EventCorrection(**body.model_dump()))
    )


@app.put("/limits/{account_id}")
def set_limit(account_id: str, body: LimitIn):
    try:
        service.set_limit(LimitRule(account_id, body.max_exposure))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"account_id": account_id, "max_exposure": body.max_exposure}


@app.put("/groups/{group_id}/limit")
def set_group_limit(group_id: str, body: LimitIn):
    try:
        service.set_group_limit(GroupRule(group_id, body.max_exposure))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"group_id": group_id, "max_exposure": body.max_exposure}


@app.put("/accounts/{account_id}/group")
def assign_account(account_id: str, body: MembershipIn):
    service.assign_account(GroupMembership(account_id, body.group_id))
    return {"account_id": account_id, "group_id": body.group_id}


@app.get("/exposure/{account_id}")
def get_exposure(account_id: str):
    return {"account_id": account_id, "exposure": service.exposure(account_id)}


@app.get("/groups/{group_id}/occupancy")
def get_group_occupancy(group_id: str):
    return service.group_occupancy(group_id)


@app.post("/stress/snapshot")
def stress_snapshot(body: ScenarioIn):
    try:
        return service.stress_snapshot(
            StressScenario(body.name, body.shocks, body.group_shocks)
        )
    except ValueError as exc:
        msg = str(exc)
        reason = msg.split(":", 1)[0] if ":" in msg else "INVALID_SCENARIO"
        raise HTTPException(
            status_code=400, detail={"reason": reason, "detail": msg}
        )
