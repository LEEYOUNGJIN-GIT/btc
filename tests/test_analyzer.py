import unittest
from decimal import Decimal

from analyzer import analyze, breakeven_sell
from backfill import OhlcvBar


def bar(index: int, low: str, high: str, close: str) -> OhlcvBar:
    return OhlcvBar(
        timestamp_ms=index * 60_000,
        open_time=f"t{index}",
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


class AnalyzerTests(unittest.TestCase):
    def test_quiet_market_uses_fee_aware_floor(self) -> None:
        bars = [bar(i, "99.99", "100.01", "100") for i in range(20)]
        levels = analyze(
            bars,
            buy_fee_rate=Decimal("0.0005"),
            sell_fee_rate=Decimal("0.0005"),
            atr_period=14,
            range_lookback=20,
        )
        self.assertEqual(levels.grid_rate, Decimal("0.002"))
        self.assertGreater(levels.grid_rate, levels.round_trip_fee_rate)
        self.assertLess(levels.support, levels.resistance)

    def test_wide_market_is_capped(self) -> None:
        bars = [bar(i, "99", "101", "100") for i in range(20)]
        levels = analyze(
            bars,
            buy_fee_rate=Decimal("0.0005"),
            sell_fee_rate=Decimal("0.0005"),
            atr_period=14,
            range_lookback=20,
            min_grid_rate=Decimal("0.002"),
            max_grid_rate=Decimal("0.004"),
        )
        self.assertEqual(levels.grid_rate, Decimal("0.004"))
        self.assertGreater(levels.atr_rate, Decimal("0.004"))

    def test_support_and_resistance_percentiles(self) -> None:
        bars = [bar(i, str(i + 1), str(i + 11), str(i + 6)) for i in range(10)]
        levels = analyze(
            bars,
            buy_fee_rate=Decimal("0.0005"),
            sell_fee_rate=Decimal("0.0005"),
            atr_period=5,
            range_lookback=10,
            support_percentile=Decimal("0"),
            resistance_percentile=Decimal("1"),
        )
        self.assertEqual(levels.support, Decimal("1"))
        self.assertEqual(levels.resistance, Decimal("20"))

    def test_breakeven_is_above_raw_fee_sum(self) -> None:
        sell = breakeven_sell(Decimal("100"), Decimal("0.0005"), Decimal("0.0005"))
        self.assertGreaterEqual(sell, Decimal("100.10"))
        self.assertLess(sell, Decimal("100.20"))


if __name__ == "__main__":
    unittest.main()
