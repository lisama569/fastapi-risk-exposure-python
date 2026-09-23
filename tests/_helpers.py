"""测试公共辅助：构造服务、统一日志打印输入事件与判定依据。"""
from __future__ import annotations

from decimal import Decimal

from app.models import Event, EventType
from app.scenarios import ScenarioEngine
from app.service import RiskService
from app.store import InMemoryStore


def fresh_stack() -> tuple[InMemoryStore, RiskService, ScenarioEngine]:
    store = InMemoryStore()
    service = RiskService(store)
    engine = ScenarioEngine(store)
    return store, service, engine


def evt(event_id: str, account_id: str, seq: int, etype: EventType | str,
        amount: str, asset_class: str = "DEFAULT") -> Event:
    return Event(
        event_id=event_id,
        account_id=account_id,
        seq=seq,
        type=EventType(etype) if isinstance(etype, str) else etype,
        amount=Decimal(amount),
        asset_class=asset_class,
    )


def log_case(title: str, payload: object, basis: object) -> None:
    """单测日志：打印输入与判定依据，便于人工复核。"""
    print(f"\n[CASE] {title}\n  输入: {payload}\n  判定依据: {basis}")
