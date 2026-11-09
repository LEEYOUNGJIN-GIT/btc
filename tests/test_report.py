import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from analyzer import close_correlation, history_stats
from backfill import OhlcvBar, load_ohlcv_csv
from report import analyze_csv, build_report


def _bar(ts: int, close: str, high: str | None = None, low: str | None = None) -> OhlcvBar:
    price = Decimal(close)
    return OhlcvBar(
        timestamp_ms=ts,
        open_time=f"t{ts}",
        open=price,
        high=Decimal(high) if high else price + 1,
        low=Decimal(low) if low else price - 1,
        close=price,
        volume=Decimal("1"),
    )


class HistoryTests(unittest.TestCase):
    def test_history_stats_return_and_drawdown(self) -> None:
        bars = [_bar(1, "100"), _bar(2, "120"), _bar(3, "90")]
        stats = history_stats(bars)
        self.assertEqual(stats.total_return, Decimal("-0.1"))
        self.assertEqual(stats.max_drawdown, (Decimal("120") - Decimal("90")) / Decimal("120"))

    def test_close_correlation_perfect(self) -> None:
        left = [_bar(1, "1"), _bar(2, "2"), _bar(3, "3")]
        right = [_bar(1, "10"), _bar(2, "20"), _bar(3, "30")]
        corr = close_correlation(left, right)
        self.assertIsNotNone(corr)
        assert corr is not None
        self.assertAlmostEqual(float(corr), 1.0, places=8)


class ReportTests(unittest.TestCase):
    def test_load_csv_and_analyze(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            minute = root / "minute"
            daily = root / "daily"
            minute.mkdir()
            daily.mkdir()
            rows = ["timestamp_ms,open_time,open,high,low,close,volume"]
            # 140 bars so atr=14 and lookback=120 fit
            for index in range(140):
                stamp = 1_700_000_000_000 + index * 60_000
                close = 100 + (index % 7)
                rows.append(
                    f"{stamp},2024-01-01T00:{index:02d}:00,{close},{close + 1},{close - 1},{close},1"
                )
            minute_csv = minute / "ohlcv_USDT-BTC.csv"
            daily_csv = daily / "ohlcv_USDT-BTC.csv"
            minute_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
            daily_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
            loaded = load_ohlcv_csv(str(minute_csv))
            self.assertEqual(len(loaded), 140)
            item = analyze_csv(
                minute_csv,
                buy_fee_rate=Decimal("0.0005"),
                sell_fee_rate=Decimal("0.0005"),
                atr_period=14,
                range_lookback=120,
                min_grid_rate=Decimal("0.002"),
                max_grid_rate=Decimal("0.004"),
            )
            self.assertEqual(item["bar_count"], 140)
            self.assertIn("grid_rate_pct", item["dynamic_levels"])
            report = build_report(
                minute_dir=minute,
                daily_dir=daily,
                buy_fee_rate=Decimal("0.0005"),
                sell_fee_rate=Decimal("0.0005"),
                atr_period=14,
                range_lookback=120,
                min_grid_rate=Decimal("0.002"),
                max_grid_rate=Decimal("0.004"),
            )
            self.assertEqual(len(report["minute"]), 1)
            self.assertEqual(len(report["daily"]), 1)


if __name__ == "__main__":
    unittest.main()
