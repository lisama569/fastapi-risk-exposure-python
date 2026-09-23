"""核心领域服务：事件幂等上报与限额并发提交。

判定规则（所有入口共享同一份内存状态，因此结论一致）：

事件上报
    1. 金额必须为有限且非负的 Decimal，否则以 ``INVALID_AMOUNT`` 拒绝；
    2. ``event_id`` 是幂等键：
       - 重复投递且载荷一致：不重复扣减，返回该事件在当前收敛状态下的判定
         （``duplicate=True``）；
       - 相同 id 但载荷冲突：以 ``DUPLICATE_CONFLICT`` 拒绝，状态不变；
    3. 全部已见事件按 (seq, event_id) 保序做贪心重放：INCREASE 总是生效；
       DECREASE 仅当释放后总敞口不为负时生效，否则暂时跳过并以
       ``NEGATIVE_EXPOSURE`` 标记。乱序到达或在先增加后补时，重新重放即可
       向“按序去重”结论收敛——重复投递、乱序或多次提交后，最终敞口与
       按序去重结果严格一致，绝不为负，也不会重复扣减。

限额提交
    在账户锁内完成“读敞口-比限额-写敞口”，并发下请求串行化保证限额永不被
    突破；``request_id`` 同样幂等，重复提交返回首次结论。
"""
from __future__ import annotations

from decimal import Decimal

from app.models import Event, EventAck, EventType, LimitCheck, LimitResult, RejectReason
from app.store import InMemoryStore

ZERO = Decimal("0")


def signed_delta(event: Event) -> Decimal:
    return event.amount if event.type == EventType.INCREASE else -event.amount


def is_valid_amount(amount: Decimal) -> bool:
    return amount.is_finite() and amount >= ZERO


def ordered_events(events: list[Event]) -> list[Event]:
    """规范事件顺序：按 (seq, event_id) 升序，乱序到达结论与此无关。"""
    return sorted(events, key=lambda e: (e.seq, e.event_id))


def greedy_replay(
    events: list[Event],
) -> tuple[Decimal, dict[str, Decimal], set[str]]:
    """保序贪心重放。

    返回 (收敛总敞口, 分资产敞口, 生效事件 id 集合)。
    会使总敞口跌破 0 的 DECREASE 被跳过（永不产生负敞口）。
    """
    total = ZERO
    per_asset: dict[str, Decimal] = {}
    active: set[str] = set()
    for event in ordered_events(events):
        delta = signed_delta(event)
        if delta < ZERO and total + delta < ZERO:
            continue  # 释放额超过在先增加：本事件暂不生效
        total += delta
        per_asset[event.asset_class] = per_asset.get(event.asset_class, ZERO) + delta
        active.add(event.event_id)
    return total, per_asset, active


class RiskService:
    """敞口聚合与限额校验的领域服务。"""

    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.store = store or InMemoryStore()
        # request_id -> 首次限额结论，保证重复提交结论稳定
        self._request_outcomes: dict[str, LimitResult] = {}

    # ---- 事件上报 -----------------------------------------------------------

    def report_event(self, event: Event) -> EventAck:
        """幂等上报一笔敞口变动事件。"""
        if not is_valid_amount(event.amount):
            return EventAck(
                event_id=event.event_id,
                account_id=event.account_id,
                accepted=False,
                duplicate=False,
                exposure_after=self.store.get_exposure(event.account_id),
                state_version=self.store.get_version(event.account_id),
                reason=RejectReason.INVALID_AMOUNT,
                detail=f"金额必须为非负有限值，收到 {event.amount}",
            )

        lock = self.store.account_lock(event.account_id)
        with lock:
            known = self.store.get_event(event.event_id)
            if known is not None and not self._same_payload(known, event):
                return EventAck(
                    event_id=event.event_id,
                    account_id=event.account_id,
                    accepted=False,
                    duplicate=True,
                    exposure_after=self.store.get_exposure(event.account_id),
                    state_version=self.store.get_version(event.account_id),
                    reason=RejectReason.DUPLICATE_CONFLICT,
                    detail=(
                        f"event_id={event.event_id} 已存在但载荷冲突："
                        f"已存 (account={known.account_id}, seq={known.seq}, "
                        f"type={known.type.value}, amount={known.amount}, "
                        f"asset={known.asset_class})"
                    ),
                )

            duplicate = known is not None
            self.store.ensure_account(event.account_id)
            if known is None:
                self.store.insert_event(event)

            # 对全部已见事件做保序贪心重放，得到收敛状态
            total, per_asset, active = greedy_replay(
                self.store.seen_events(event.account_id)
            )
            self.store.replace_exposure(event.account_id, total, per_asset)

            accepted = event.event_id in active
            if accepted:
                ack = EventAck(
                    event_id=event.event_id,
                    account_id=event.account_id,
                    accepted=True,
                    duplicate=duplicate,
                    exposure_after=total,
                    state_version=self.store.get_version(event.account_id),
                    detail=(
                        f"事件按 seq={event.seq} 生效（{'重复投递未重复扣减' if duplicate else '首次入账'}），"
                        f"收敛敞口 {total}"
                    ),
                )
            else:
                ack = EventAck(
                    event_id=event.event_id,
                    account_id=event.account_id,
                    accepted=False,
                    duplicate=duplicate,
                    exposure_after=total,
                    state_version=self.store.get_version(event.account_id),
                    reason=RejectReason.NEGATIVE_EXPOSURE,
                    detail=(
                        f"按 (seq,event_id) 保序重放时，该 DECREASE {event.amount} "
                        f"超出在先 INCREASE 形成的可用敞口，暂不生效；"
                        f"当前收敛敞口 {total}（未产生负敞口）"
                    ),
                )
            return ack

    @staticmethod
    def _same_payload(a: Event, b: Event) -> bool:
        return (
            a.account_id == b.account_id
            and a.seq == b.seq
            and a.type == b.type
            and a.amount == b.amount
            and a.asset_class == b.asset_class
        )

    # ---- 限额校验 -----------------------------------------------------------

    def check_limit(self, req: LimitCheck) -> LimitResult:
        """基于当前敞口做限额校验并提交占用。"""
        lock = self.store.account_lock(req.account_id)
        with lock:
            cached = self._request_outcomes.get(req.request_id)
            if cached is not None:
                return cached

            exposure = self.store.get_exposure(req.account_id)
            version = self.store.get_version(req.account_id)

            def store_result(result: LimitResult) -> LimitResult:
                self._request_outcomes[req.request_id] = result
                return result

            if not is_valid_amount(req.amount):
                return store_result(LimitResult(
                    request_id=req.request_id,
                    account_id=req.account_id,
                    approved=False,
                    exposure_before=exposure,
                    exposure_after=exposure,
                    limit=self.store.get_limit(req.account_id) or ZERO,
                    state_version=version,
                    reason=RejectReason.INVALID_AMOUNT,
                    detail=f"申请金额必须为非负有限值，收到 {req.amount}",
                ))

            if not self.store.has_account(req.account_id):
                return store_result(LimitResult(
                    request_id=req.request_id,
                    account_id=req.account_id,
                    approved=False,
                    exposure_before=ZERO,
                    exposure_after=ZERO,
                    limit=ZERO,
                    state_version=0,
                    reason=RejectReason.ACCOUNT_NOT_FOUND,
                    detail=f"账户 {req.account_id} 缺少基准数据，未建立限额台账",
                ))

            limit = self.store.get_limit(req.account_id)
            if limit is None:
                return store_result(LimitResult(
                    request_id=req.request_id,
                    account_id=req.account_id,
                    approved=False,
                    exposure_before=exposure,
                    exposure_after=exposure,
                    limit=ZERO,
                    state_version=version,
                    reason=RejectReason.ACCOUNT_NOT_FOUND,
                    detail=f"账户 {req.account_id} 未配置限额，拒绝静默使用默认值",
                ))

            projected = exposure + req.amount
            if projected > limit:
                return store_result(LimitResult(
                    request_id=req.request_id,
                    account_id=req.account_id,
                    approved=False,
                    exposure_before=exposure,
                    exposure_after=exposure,
                    limit=limit,
                    state_version=version,
                    reason=RejectReason.LIMIT_EXCEEDED,
                    detail=(
                        f"当前敞口 {exposure} + 申请 {req.amount} = {projected} "
                        f"> 限额 {limit}（剩余额度 {limit - exposure}），未占用任何额度"
                    ),
                ))

            self.store.apply_delta(req.account_id, req.amount, "DEFAULT")
            return store_result(LimitResult(
                request_id=req.request_id,
                account_id=req.account_id,
                approved=True,
                exposure_before=exposure,
                exposure_after=projected,
                limit=limit,
                state_version=self.store.get_version(req.account_id),
                detail=f"已占用 {req.amount}，占用后敞口 {projected} <= 限额 {limit}",
            ))

    # ---- 重放 ---------------------------------------------------------------

    def replay(self, account_id: str) -> Decimal:
        """按 (seq, event_id) 重放全部已见事件，返回收敛敞口。"""
        with self.store.account_lock(account_id):
            total, _, _ = greedy_replay(self.store.seen_events(account_id))
            return total
