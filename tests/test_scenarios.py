"""压力情景重算：可复现、非法情景拒绝、缺失基准拒绝。"""
import unittest
from decimal import Decimal

from app.models import EventType, RejectReason, ScenarioSpec
from app.scenarios import ScenarioValidationError
from tests._helpers import evt, fresh_stack, log_case


class ScenarioTest(unittest.TestCase):
    def _feed(self, service) -> None:
        for e in [
            evt("e1", "acct-1", 1, EventType.INCREASE, "100", "EQUITY"),
            evt("e2", "acct-1", 2, EventType.INCREASE, "200", "BOND"),
            evt("e3", "acct-1", 3, EventType.DECREASE, "20", "EQUITY"),
        ]:
            self.assertTrue(service.report_event(e).accepted)

    def test_stress_recomputation_reproducible(self) -> None:
        store, service, engine = fresh_stack()
        store.ensure_account("acct-1", Decimal("9999"))
        self._feed(service)
        spec = ScenarioSpec(
            scenario_id="crash",
            haircuts={"EQUITY": Decimal("0.5"), "BOND": Decimal("0.9")},
            scenario_limit=Decimal("300"),
        )
        engine.validate_spec(spec)
        store.put_scenario(spec)

        r1 = engine.evaluate("acct-1", "crash")
        r2 = engine.evaluate("acct-1", "crash")
        # EQUITY 80 * 0.5 = 40; BOND 200 * 0.9 = 180 -> 220
        log_case(
            "crash 情景重算（EQUITY 80, BOND 200）",
            "haircuts EQUITY=0.5,BOND=0.9; limit=300",
            f"baseline={r1.baseline_exposure} stressed={r1.stressed_exposure} "
            f"within={r1.within_limit} per_asset={r1.per_asset}",
        )
        self.assertEqual(r1.stressed_exposure, Decimal("220.0"))
        self.assertEqual(r1.baseline_exposure, Decimal("280"))
        self.assertTrue(r1.within_limit)
        self.assertEqual(r1, r2)  # 同情景同事件序列，快照完全一致

    def test_order_independent_reproducibility(self) -> None:
        """事件乱序喂入，重算结论与按序喂入一致。"""
        spec = ScenarioSpec(scenario_id="shock",
                            haircuts={"CASH": Decimal("0.25")})

        def build_and_eval(order: list[int]):
            store, service, engine = fresh_stack()
            store.ensure_account("A", None)
            events = [
                evt("1", "A", 1, EventType.INCREASE, "100", "CASH"),
                evt("2", "A", 2, EventType.INCREASE, "60", "CASH"),
                evt("3", "A", 3, EventType.DECREASE, "40", "CASH"),
            ]
            for i in order:
                service.report_event(events[i])
            store.put_scenario(spec)
            return engine.evaluate("A", "shock")

        ordered = build_and_eval([0, 1, 2])
        shuffled = build_and_eval([2, 0, 1])
        log_case(
            "按序 vs 乱序喂入的情景重算",
            "order=[1,2,3] vs [3,1,2]",
            f"ordered.stressed={ordered.stressed_exposure}; "
            f"shuffled.stressed={shuffled.stressed_exposure}",
        )
        self.assertEqual(ordered.stressed_exposure, shuffled.stressed_exposure)
        self.assertEqual(ordered.stressed_exposure, Decimal("30.00"))

    def test_scenario_limit_breach_flagged(self) -> None:
        store, service, engine = fresh_stack()
        store.ensure_account("acct-1", None)
        self._feed(service)
        spec = ScenarioSpec(
            scenario_id="tight",
            haircuts={"EQUITY": Decimal("1"), "BOND": Decimal("1")},
            scenario_limit=Decimal("100"),
        )
        store.put_scenario(spec)
        r = engine.evaluate("acct-1", "tight")
        log_case("情景限额突破", "haircut=1.0; scenario_limit=100",
                 f"stressed={r.stressed_exposure} within={r.within_limit} reason={r.reason}")
        self.assertFalse(r.within_limit)
        self.assertEqual(r.reason, RejectReason.SCENARIO_LIMIT_EXCEEDED)

    def test_unclassified_asset_rejected(self) -> None:
        store, service, engine = fresh_stack()
        store.ensure_account("acct-1", None)
        self._feed(service)
        spec = ScenarioSpec(scenario_id="partial",
                            haircuts={"EQUITY": Decimal("0.5")})  # 缺 BOND
        store.put_scenario(spec)
        r = engine.evaluate("acct-1", "partial")
        log_case("资产类别缺少折扣率", "只给 EQUITY 折扣，事件含 BOND",
                 f"reason={r.reason} detail={r.detail}")
        self.assertEqual(r.reason, RejectReason.UNCLASSIFIED_ASSET)

    def test_missing_baseline_rejected(self) -> None:
        store, _, engine = fresh_stack()
        spec = ScenarioSpec(scenario_id="x", haircuts={"CASH": Decimal("1")})
        store.put_scenario(spec)
        r = engine.evaluate("ghost", "x")
        log_case("缺失基准账户", "账户 ghost 从未建账", f"reason={r.reason}")
        self.assertEqual(r.reason, RejectReason.MISSING_BASELINE)

    def test_unknown_scenario_rejected(self) -> None:
        store, _, engine = fresh_stack()
        store.ensure_account("acct-1", None)
        r = engine.evaluate("acct-1", "nope")
        log_case("未知情景", "scenario_id=nope", f"reason={r.reason}")
        self.assertEqual(r.reason, RejectReason.SCENARIO_NOT_FOUND)

    def test_invalid_scenario_params(self) -> None:
        _, _, engine = fresh_stack()
        bad_cases = [
            ("空折扣映射", ScenarioSpec(scenario_id="s", haircuts={})),
            ("折扣率>1", ScenarioSpec(scenario_id="s",
                                      haircuts={"CASH": Decimal("1.2")})),
            ("折扣率<0", ScenarioSpec(scenario_id="s",
                                      haircuts={"CASH": Decimal("-0.1")})),
            ("负情景限额", ScenarioSpec(scenario_id="s",
                                        haircuts={"CASH": Decimal("1")},
                                        scenario_limit=Decimal("-5"))),
        ]
        for name, spec in bad_cases:
            with self.subTest(name=name):
                with self.assertRaises(ScenarioValidationError):
                    engine.validate_spec(spec)
        log_case("非法情景参数", [n for n, _ in bad_cases],
                 "全部抛出 ScenarioValidationError，不使用默认值")


if __name__ == "__main__":
    unittest.main()
