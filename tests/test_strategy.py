import unittest
from decimal import Decimal

from analyzer import DynamicLevels
from strategy import (
    Candle,
    MarketSnapshot,
    Strategy,
    StrategyParams,
    align_price,
    box_from_candles,
    net_pnl,
    usdt_tick,
)


def params(**overrides: object) -> StrategyParams:
    values: dict[str, object] = dict(
        buy_fee_rate=Decimal("0.0005"),
        sell_fee_rate=Decimal("0.0005"),
        target_profit_rate=Decimal("0.0025"),
        stop_loss_rate=Decimal("0.005"),
        buy_offset_rate=Decimal("0.001"),
        entry_zone=Decimal("0.40"),
        breakdown_buffer=Decimal("0.002"),
        min_order_quote=Decimal("0.5"),
        balance_use_ratio=Decimal("1"),
        quote_reserve=Decimal("0"),
        volume_precision=8,
    )
    values.update(overrides)
    built = StrategyParams(**values)  # type: ignore[arg-type]
    built.validate()
    return built


class TickTests(unittest.TestCase):
    def test_usdt_tick_table(self) -> None:
        self.assertEqual(usdt_tick(Decimal("79465.24")), Decimal("0.01"))
        self.assertEqual(usdt_tick(Decimal("10")), Decimal("0.01"))
        self.assertEqual(usdt_tick(Decimal("9.999")), Decimal("0.001"))
        self.assertEqual(usdt_tick(Decimal("1")), Decimal("0.001"))
        self.assertEqual(usdt_tick(Decimal("0.25")), Decimal("0.0001"))
        self.assertEqual(usdt_tick(Decimal("0.05")), Decimal("0.00001"))
        self.assertEqual(usdt_tick(Decimal("0.005")), Decimal("0.000001"))
        self.assertEqual(usdt_tick(Decimal("0.0005")), Decimal("0.0000001"))
        self.assertEqual(usdt_tick(Decimal("0.00009")), Decimal("0.00000001"))

    def test_align_floor_and_boundary_ceil(self) -> None:
        self.assertEqual(align_price(Decimal("79465.249"), "floor"), Decimal("79465.24"))
        self.assertEqual(align_price(Decimal("100.35030015"), "ceil"), Decimal("100.36"))
        self.assertEqual(align_price(Decimal("9.9991"), "ceil"), Decimal("10"))


class BreakEvenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = Strategy(params())

    def test_min_sell_clears_fee_and_target(self) -> None:
        buy = Decimal("100")
        sell = self.strategy.min_sell_price(buy)
        self.assertEqual(sell, Decimal("100.36"))
        additive = buy * (1 + Decimal("0.0005") + Decimal("0.0005") + Decimal("0.0025"))
        self.assertGreaterEqual(sell, additive)
        cost = buy * Decimal("1.0005")
        proceeds = sell * Decimal("0.9995")
        self.assertGreaterEqual(proceeds, cost * Decimal("1.0025"))

    def test_stop_is_below_buy(self) -> None:
        stop = self.strategy.stop_price(Decimal("100"))
        self.assertEqual(stop, Decimal("99.50"))
        self.assertLessEqual(stop, Decimal("100") * Decimal("0.995"))
        self.assertTrue(self.strategy.should_stop(stop, stop))
        self.assertFalse(self.strategy.should_stop(stop + Decimal("0.01"), stop))

    def test_reprice_never_undercuts_minimum(self) -> None:
        minimum = self.strategy.min_sell_price(Decimal("100"))
        self.assertIsNone(self.strategy.next_sell_price(minimum, Decimal("100"), minimum))
        moved = self.strategy.next_sell_price(Decimal("101"), Decimal("100.50"), minimum)
        self.assertEqual(moved, Decimal("100.50"))
        self.assertGreaterEqual(moved, minimum)
        restored = self.strategy.next_sell_price(Decimal("100.30"), Decimal("100.80"), minimum)
        self.assertEqual(restored, minimum)

    def test_net_pnl(self) -> None:
        net, ratio = net_pnl(Decimal("100"), Decimal("0.05"), Decimal("100.40"), Decimal("0.05"))
        self.assertEqual(net, Decimal("0.30"))
        self.assertEqual(ratio, Decimal("0.30") / Decimal("100.05"))


class EntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = Strategy(params())

    def _snap(self, trade: str, bid: str, ask: str, low: str = "99", high: str = "101") -> MarketSnapshot:
        return MarketSnapshot(
            trade_price=Decimal(trade),
            best_bid=Decimal(bid),
            best_ask=Decimal(ask),
            bid_size=Decimal("1"),
            ask_size=Decimal("1"),
            box_low=Decimal(low),
            box_high=Decimal(high),
        )

    def test_buys_near_box_low_with_full_quote(self) -> None:
        decision = self.strategy.plan_buy(self._snap("99.20", "99.20", "99.22"), Decimal("1000"))
        self.assertIsNotNone(decision.plan)
        assert decision.plan is not None
        self.assertEqual(decision.plan.price, Decimal("99.10"))
        cost = decision.plan.notional + decision.plan.fee_reserve
        self.assertLessEqual(cost, Decimal("1000"))
        self.assertGreater(cost, Decimal("999.9"))
        self.assertGreaterEqual(decision.plan.notional, Decimal("0.5"))

    def test_skips_upper_box_and_breakdown(self) -> None:
        upper = self.strategy.plan_buy(self._snap("100.80", "100.80", "100.82"), Decimal("1000"))
        self.assertIsNone(upper.plan)
        self.assertIn("상단", upper.skip_reason or "")
        broken = self.strategy.plan_buy(self._snap("98", "98", "98.01"), Decimal("1000"))
        self.assertIsNone(broken.plan)
        self.assertIn("이탈", broken.skip_reason or "")

    def test_small_balance_is_rejected(self) -> None:
        decision = self.strategy.plan_buy(self._snap("99.20", "99.20", "99.22"), Decimal("0.4"))
        self.assertIsNone(decision.plan)

    def test_dynamic_grid_replaces_fixed_percent(self) -> None:
        levels = DynamicLevels(
            support=Decimal("99"),
            resistance=Decimal("101"),
            grid_rate=Decimal("0.003"),
            atr=Decimal("0.30"),
            atr_rate=Decimal("0.003"),
            mean_range=Decimal("0.30"),
            mean_range_rate=Decimal("0.003"),
            round_trip_fee_rate=Decimal("0.001"),
            sample_size=30,
            last_close=Decimal("99.20"),
        )
        self.strategy.update_levels(levels)
        decision = self.strategy.plan_buy(self._snap("99.20", "99.20", "99.22"), Decimal("1000"))
        assert decision.plan is not None
        self.assertEqual(decision.plan.price, Decimal("99"))
        self.assertIn("그리드", decision.plan.note)
        sell = self.strategy.min_sell_price(Decimal("100"))
        self.assertEqual(sell, Decimal("100.30"))
        cost = Decimal("100") * Decimal("1.0005")
        proceeds = sell * Decimal("0.9995")
        self.assertGreater(proceeds, cost)

    def test_box_from_candles(self) -> None:
        low, high = box_from_candles(
            [
                Candle(Decimal("100"), Decimal("105"), Decimal("99"), Decimal("101")),
                Candle(Decimal("101"), Decimal("103"), Decimal("97"), Decimal("100")),
            ]
        )
        self.assertEqual(low, Decimal("97"))
        self.assertEqual(high, Decimal("105"))


if __name__ == "__main__":
    unittest.main()
