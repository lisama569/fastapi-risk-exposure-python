"""FastAPI 入口：事件上报、限额校验、情景重算的 HTTP 适配层。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.models import Decision, ExposureEvent, LimitRule, StressScenario
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


class ScenarioIn(BaseModel):
    name: str
    shocks: dict[str, float]


@app.post("/events")
def submit_event(body: EventIn):
    record = service.submit_event(ExposureEvent(**body.model_dump()))
    status = 200 if record.decision is not Decision.REJECTED else 409
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status,
        content={
            "decision": record.decision.value,
            "reason": record.reason.value,
            "detail": record.detail,
        },
    )


@app.put("/limits/{account_id}")
def set_limit(account_id: str, body: LimitIn):
    try:
        service.set_limit(LimitRule(account_id, body.max_exposure))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"account_id": account_id, "max_exposure": body.max_exposure}


@app.get("/exposure/{account_id}")
def get_exposure(account_id: str):
    return {"account_id": account_id, "exposure": service.exposure(account_id)}


@app.post("/stress/snapshot")
def stress_snapshot(body: ScenarioIn):
    try:
        return service.stress_snapshot(StressScenario(body.name, body.shocks))
    except ValueError as exc:
        msg = str(exc)
        reason = msg.split(":", 1)[0] if ":" in msg else "INVALID_SCENARIO"
        raise HTTPException(
            status_code=400, detail={"reason": reason, "detail": msg}
        )
