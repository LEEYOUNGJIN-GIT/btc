import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from backfill import CandleStore, OhlcvBar, backfill_ohlcv


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
            self.assertEqual(first.pages, 2)
            self.assertEqual(first.new_rows, 400)
            self.assertEqual(store.count("USDT-BTC", 1), 400)
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
            self.assertEqual(store.count("USDT-BTC", 1), 400)
            csv_path = Path(folder) / "ohlcv.csv"
            self.assertEqual(store.export_csv("USDT-BTC", 1, str(csv_path)), 400)
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


if __name__ == "__main__":
    unittest.main()
