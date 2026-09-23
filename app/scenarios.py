"""压力情景重算与快照。

规则：
- 情景参数必须显式合法：每个资产类别折扣率为 [0, 1] 内的有限 Decimal；
  非法参数抛出 ``ValueError``，由入口明确拒绝，绝不静默使用默认值。
- 重算所需基准（账户、已接受事件涉及的资产类别折扣率、基准敞口）缺一不可，
  缺失即以区分化原因拒绝：``MISSING_BASELINE`` / ``UNCLASSIFIED_ASSET``。
- 重算是纯函数式计算：相同情景定义 + 相同有序事件序列 ⇒ 相同快照，
  与到达顺序、调用次数无关。
"""
from __future__ import annotations

import threading
from decimal import Decimal

from app.models import RejectReason, ScenarioResult, ScenarioSpec
from app.service import greedy_replay
from app.store import InMemoryStore

ZERO = Decimal("0")
ONE = Decimal("1")


class ScenarioValidationError(ValueError):
    """非法情景参数。"""


class ScenarioEngine:
    """按情景折扣率重算敞口并产出可复现快照。"""

    def __init__(self, store: InMemoryStore) -> None:
        self.store = store
        self._snapshots: dict[tuple[str, str], ScenarioResult] = {}
        self._lock = threading.RLock()

    def validate_spec(self, spec: ScenarioSpec) -> None:
        """校验情景参数，非法则抛出 :class:`ScenarioValidationError`。"""
        if not spec.scenario_id or not spec.scenario_id.strip():
            raise ScenarioValidationError("scenario_id 不能为空")
        if not isinstance(spec.haircuts, dict) or not spec.haircuts:
            raise ScenarioValidationError("haircuts 必须为非空的 {资产类别: 折扣率} 映射")
        for asset, haircut in spec.haircuts.items():
            if not isinstance(asset, str) or not asset.strip():
                raise ScenarioValidationError("haircuts 的资产类别键必须为非空字符串")
            if not isinstance(haircut, Decimal) or not haircut.is_finite():
                raise ScenarioValidationError(f"资产类别 {asset} 的折扣率必须为有限数值")
            if haircut < ZERO or haircut > ONE:
                raise ScenarioValidationError(
                    f"资产类别 {asset} 的折扣率 {haircut} 超出 [0, 1] 区间"
                )
        if spec.scenario_limit is not None:
            limit = spec.scenario_limit
            if not limit.is_finite() or limit < ZERO:
                raise ScenarioValidationError(f"情景限额必须为非负有限值，收到 {limit}")

    def evaluate(self, account_id: str, scenario_id: str) -> ScenarioResult:
        """重算指定账户在指定情景下的压力敞口并留存快照。"""
        with self.store.account_lock(account_id):
            version = self.store.get_version(account_id)
            baseline = self.store.get_exposure(account_id)

            spec = self.store.get_scenario(scenario_id)
            if spec is None:
                result = ScenarioResult(
                    scenario_id=scenario_id,
                    account_id=account_id,
                    baseline_exposure=baseline,
                    stressed_exposure=ZERO,
                    limit=None,
                    within_limit=None,
                    applied_events=0,
                    state_version=version,
                    reason=RejectReason.SCENARIO_NOT_FOUND,
                    detail=f"情景 {scenario_id} 未注册，拒绝静默使用默认情景",
                )
                return self._save(result)

            if not self.store.has_account(account_id):
                result = ScenarioResult(
                    scenario_id=scenario_id,
                    account_id=account_id,
                    baseline_exposure=ZERO,
                    stressed_exposure=ZERO,
                    limit=spec.scenario_limit,
                    within_limit=None,
                    applied_events=0,
                    state_version=0,
                    reason=RejectReason.MISSING_BASELINE,
                    detail=f"账户 {account_id} 缺少基准数据，无法重算",
                )
                return self._save(result)

            # 保序贪心重放：压力重算只针对生效事件，结果天然可复现
            baseline, per_asset, active_ids = greedy_replay(
                self.store.seen_events(account_id)
            )
            seen = self.store.seen_events(account_id)
            events = [e for e in seen if e.event_id in active_ids]
            asset_classes = {e.asset_class for e in events}
            for asset, value in per_asset.items():
                if value != ZERO:
                    asset_classes.add(asset)

            missing = sorted(a for a in asset_classes if a not in spec.haircuts)
            if missing:
                result = ScenarioResult(
                    scenario_id=scenario_id,
                    account_id=account_id,
                    baseline_exposure=baseline,
                    stressed_exposure=ZERO,
                    limit=spec.scenario_limit,
                    within_limit=None,
                    applied_events=len(events),
                    state_version=version,
                    reason=RejectReason.UNCLASSIFIED_ASSET,
                    detail=(
                        f"资产类别 {missing} 在情景 {scenario_id} 中未配置折扣率，"
                        "拒绝静默套用默认折扣"
                    ),
                )
                return self._save(result)

            stressed_per_asset: dict[str, Decimal] = {}
            stressed_total = ZERO
            for asset in sorted(asset_classes):
                base = per_asset.get(asset, ZERO)
                stressed = base * spec.haircuts[asset]
                stressed_per_asset[asset] = stressed
                stressed_total += stressed

            within_limit: bool | None = None
            reason: RejectReason | None = None
            detail = (
                f"按 {sorted(asset_classes)} 的折扣率重算：基准 {baseline} "
                f"-> 压力后 {stressed_total}（生效事件 {len(events)}，"
                f"状态版本 {version}）"
            )
            if spec.scenario_limit is not None:
                within_limit = stressed_total <= spec.scenario_limit
                if not within_limit:
                    reason = RejectReason.SCENARIO_LIMIT_EXCEEDED
                    detail = (
                        f"压力后敞口 {stressed_total} > 情景限额 "
                        f"{spec.scenario_limit}；" + detail
                    )

            result = ScenarioResult(
                scenario_id=scenario_id,
                account_id=account_id,
                baseline_exposure=baseline,
                stressed_exposure=stressed_total,
                limit=spec.scenario_limit,
                within_limit=within_limit,
                applied_events=len(events),
                state_version=version,
                reason=reason,
                detail=detail,
                per_asset=stressed_per_asset,
            )
            return self._save(result)

    def _save(self, result: ScenarioResult) -> ScenarioResult:
        with self._lock:
            self._snapshots[(result.scenario_id, result.account_id)] = result
        return result

    def last_snapshot(self, scenario_id: str, account_id: str) -> ScenarioResult | None:
        with self._lock:
            return self._snapshots.get((scenario_id, account_id))
