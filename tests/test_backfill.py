import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from backfill import (
    DAY_UNIT,
    CandleStore,
    OhlcvBar,
    backfill_markets,
    backfill_ohlcv,
    csv_path_for_market,
    ensure_market,
    export_markets_csv,
    parse_markets,
)


def bars_from(start_ms: int, count: int) -> list[OhlcvBar]:
    rows = []
    for offset in range(count):
        stamp = start_ms - offset * 60_000
        rows.append(
            OhlcvBar(
                timestamp_ms=stamp,
                open_time=f"T{stamp}",
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=Decimal("2"),
            )
        )
    return rows


class BackfillTests(unittest.TestCase):
    def test_pages_until_start_and_second_run_is_incremental(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "ohlcv.sqlite")
            store = CandleStore(path)
            now = 1_000_000_000_000
            pages = {
                None: bars_from(now, 200),
            }
            first_oldest = pages[None][-1].open_time
            pages[first_oldest] = bars_from(pages[None][-1].timestamp_ms - 60_000, 200)
            calls: list[str | None] = []

            def fetch(market: str, unit: int, count: int, to: str | None) -> list[OhlcvBar]:
                self.assertEqual(market, "USDT-BTC")
                self.assertEqual(unit, 1)
                self.assertEqual(count, 200)
                calls.append(to)
                return pages[to]

            first = backfill_ohlcv(
                fetch,
                store,
                "USDT-BTC",
                1,
                hours=6,
                now_ms=now,
                page_sleep=0,
                retry_delay=0,
                sleep=lambda _seconds: None,
            )
            start_ms = now - 6 * 3_600_000
            kept = store.count("USDT-BTC", 1)
            self.assertEqual(first.pages, 2)
            self.assertEqual(first.new_rows, 400)
            self.assertEqual(first.stored_rows, kept)
            self.assertLessEqual(kept, 400)
            self.assertTrue(all(bar.timestamp_ms >= start_ms for bar in store.load("USDT-BTC", 1)))
            self.assertEqual(calls, [None, first_oldest])

            calls.clear()
            second = backfill_ohlcv(
                fetch,
                store,
                "USDT-BTC",
                1,
                hours=6,
                now_ms=now,
                page_sleep=0,
                retry_delay=0,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(second.pages, 1)
            self.assertEqual(second.new_rows, 0)
            self.assertEqual(store.count("USDT-BTC", 1), kept)
            csv_path = Path(folder) / "ohlcv.csv"
            self.assertEqual(store.export_csv("USDT-BTC", 1, str(csv_path)), kept)
            header = csv_path.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(header, "timestamp_ms,open_time,open,high,low,close,volume")
            store.close()

    def test_retries_failed_page(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = CandleStore(str(Path(folder) / "ohlcv.sqlite"))
            state = {"fails": 2}

            def fetch(market: str, unit: int, count: int, to: str | None) -> list[OhlcvBar]:
                if state["fails"]:
                    state["fails"] -= 1
                    raise ConnectionError("temporary")
                return bars_from(5_000_000, 3)

            slept: list[float] = []
            result = backfill_ohlcv(
                fetch,
                store,
                "USDT-BTC",
                1,
                hours=1,
                now_ms=5_000_000,
                page_sleep=0,
                max_retries=4,
                retry_delay=0.25,
                sleep=slept.append,
            )
            self.assertEqual(result.stored_rows, 3)
            self.assertEqual(slept, [0.25, 0.5])
            store.close()

    def test_empty_page_stops_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = CandleStore(str(Path(folder) / "ohlcv.sqlite"))
            result = backfill_ohlcv(
                lambda *_args: [],
                store,
                "USDT-BTC",
                1,
                hours=1,
                now_ms=5_000_000,
                page_sleep=0,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(result.pages, 0)
            self.assertEqual(result.stored_rows, 0)
            store.close()

    def test_parse_markets_defaults_to_btc_and_eth(self) -> None:
        self.assertEqual(parse_markets(""), ("USDT-BTC", "USDT-ETH"))
        self.assertEqual(parse_markets("usdt-btc; usdt-eth,USDT-BTC"), ("USDT-BTC", "USDT-ETH"))
        self.assertEqual(ensure_market(("USDT-ETH",), "USDT-BTC"), ("USDT-BTC", "USDT-ETH"))

    def test_backfill_markets_stores_btc_and_eth(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = CandleStore(str(Path(folder) / "ohlcv.sqlite"))
            now = 2_000_000_000_000
            seen: list[str] = []

            def fetch(market: str, unit: int, count: int, to: str | None) -> list[OhlcvBar]:
                self.assertEqual(unit, 1)
                if to is not None:
                    return []
                seen.append(market)
                base = Decimal("100") if market == "USDT-BTC" else Decimal("10")
                return [
                    OhlcvBar(
                        timestamp_ms=now - offset * 60_000,
                        open_time=f"{market}-{offset}",
                        open=base,
                        high=base + 1,
                        low=base - 1,
                        close=base,
                        volume=Decimal("1"),
                    )
                    for offset in range(3)
                ]

            results = backfill_markets(
                fetch,
                store,
                ("USDT-BTC", "USDT-ETH"),
                1,
                hours=1,
                now_ms=now,
                page_sleep=0,
                market_sleep=0,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(seen, ["USDT-BTC", "USDT-ETH"])
            self.assertEqual(results["USDT-BTC"].stored_rows, 3)
            self.assertEqual(results["USDT-ETH"].stored_rows, 3)
            self.assertEqual(store.count("USDT-BTC", 1), 3)
            self.assertEqual(store.count("USDT-ETH", 1), 3)
            csv_root = str(Path(folder) / "ohlcv.csv")
            written = export_markets_csv(store, ("USDT-BTC", "USDT-ETH"), 1, csv_root)
            self.assertEqual(written["USDT-BTC"], 3)
            self.assertEqual(written["USDT-ETH"], 3)
            self.assertTrue(Path(csv_path_for_market(csv_root, "USDT-BTC")).exists())
            self.assertTrue(Path(csv_path_for_market(csv_root, "USDT-ETH")).exists())
            store.close()

    def test_prune_removes_rows_before_window(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = CandleStore(str(Path(folder) / "ohlcv.sqlite"))
            now = 2_000_000_000_000
            old = OhlcvBar(now - 10_000_000, "old", Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"))
            fresh = OhlcvBar(now - 1_000, "fresh", Decimal("2"), Decimal("2"), Decimal("2"), Decimal("2"), Decimal("2"))
            store.upsert("USDT-BTC", 1, [old, fresh])
            self.assertEqual(store.count("USDT-BTC", 1), 2)
            removed = store.prune("USDT-BTC", 1, now - 5_000)
            self.assertEqual(removed, 1)
            self.assertEqual(store.count("USDT-BTC", 1), 1)
            store.close()

    def test_daily_backfill_uses_day_unit(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = CandleStore(str(Path(folder) / "daily.sqlite"))
            now = 1_700_000_000_000
            units: list[int] = []

            def fetch(market: str, unit: int, count: int, to: str | None) -> list[OhlcvBar]:
                units.append(unit)
                if to is not None:
                    return []
                return [
                    OhlcvBar(
                        timestamp_ms=now - offset * 86_400_000,
                        open_time=f"2024-01-{offset + 1:02d}T00:00:00",
                        open=Decimal("1"),
                        high=Decimal("2"),
                        low=Decimal("0.5"),
                        close=Decimal("1.5"),
                        volume=Decimal("10"),
                    )
                    for offset in range(5)
                ]

            result = backfill_ohlcv(
                fetch,
                store,
                "USDT-BTC",
                unit=1,
                days=5,
                now_ms=now,
                page_sleep=0,
                sleep=lambda _seconds: None,
            )
            self.assertTrue(units)
            self.assertTrue(all(value == DAY_UNIT for value in units))
            self.assertEqual(result.stored_rows, 5)
            self.assertEqual(store.count("USDT-BTC", DAY_UNIT), 5)
            store.close()


if __name__ == "__main__":
    unittest.main()
