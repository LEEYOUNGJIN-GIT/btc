"""업비트 분봉을 페이지 단위로 받아 SQLite 에 쌓습니다.

캔들 REST 는 한 번에 최대 200개입니다. 24시간~7일치는 여러 번 나눠 받고,
이미 저장한 시각은 다시 끝까지 당기지 않습니다. 요청 사이에는 쉬고,
오류가 나면 지수 간격으로 재시도합니다.
"""

from __future__ import annotations

import csv
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

from logger import get_logger

logger = get_logger()

PageFetcher = Callable[[str, int, int, str | None], Sequence["OhlcvBar"]]


@dataclass(frozen=True)
class OhlcvBar:
    timestamp_ms: int
    open_time: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class BackfillResult:
    pages: int
    new_rows: int
    stored_rows: int
    oldest_ms: int | None
    newest_ms: int | None


class CandleStore:
    def __init__(self, path: str):
        target = Path(path)
        if target.parent and str(target.parent) not in {"", "."}:
            target.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(target)
        self._conn = sqlite3.connect(self.path, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candles (
                market TEXT NOT NULL,
                unit INTEGER NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                open_time TEXT NOT NULL,
                open TEXT NOT NULL,
                high TEXT NOT NULL,
                low TEXT NOT NULL,
                close TEXT NOT NULL,
                volume TEXT NOT NULL,
                PRIMARY KEY (market, unit, timestamp_ms)
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CandleStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def count(self, market: str, unit: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM candles WHERE market = ? AND unit = ?",
            (market, unit),
        ).fetchone()
        return int(row[0] if row else 0)

    def coverage(self, market: str, unit: int) -> tuple[int | None, int | None, int]:
        row = self._conn.execute(
            """
            SELECT MIN(timestamp_ms), MAX(timestamp_ms), COUNT(*)
            FROM candles WHERE market = ? AND unit = ?
            """,
            (market, unit),
        ).fetchone()
        if row is None or row[2] == 0:
            return None, None, 0
        return int(row[0]), int(row[1]), int(row[2])

    def upsert(self, market: str, unit: int, bars: Sequence[OhlcvBar]) -> int:
        before = self.count(market, unit)
        self._conn.executemany(
            """
            INSERT INTO candles (
                market, unit, timestamp_ms, open_time, open, high, low, close, volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market, unit, timestamp_ms) DO UPDATE SET
                open_time = excluded.open_time,
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume
            """,
            [
                (
                    market,
                    unit,
                    bar.timestamp_ms,
                    bar.open_time,
                    format(bar.open, "f"),
                    format(bar.high, "f"),
                    format(bar.low, "f"),
                    format(bar.close, "f"),
                    format(bar.volume, "f"),
                )
                for bar in bars
            ],
        )
        self._conn.commit()
        return self.count(market, unit) - before

    def load(self, market: str, unit: int, start_ms: int | None = None) -> list[OhlcvBar]:
        if start_ms is None:
            rows = self._conn.execute(
                """
                SELECT timestamp_ms, open_time, open, high, low, close, volume
                FROM candles WHERE market = ? AND unit = ?
                ORDER BY timestamp_ms ASC
                """,
                (market, unit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT timestamp_ms, open_time, open, high, low, close, volume
                FROM candles
                WHERE market = ? AND unit = ? AND timestamp_ms >= ?
                ORDER BY timestamp_ms ASC
                """,
                (market, unit, start_ms),
            ).fetchall()
        return [
            OhlcvBar(
                timestamp_ms=int(row[0]),
                open_time=str(row[1]),
                open=Decimal(row[2]),
                high=Decimal(row[3]),
                low=Decimal(row[4]),
                close=Decimal(row[5]),
                volume=Decimal(row[6]),
            )
            for row in rows
        ]

    def export_csv(self, market: str, unit: int, path: str, start_ms: int | None = None) -> int:
        bars = self.load(market, unit, start_ms)
        target = Path(path)
        if target.parent and str(target.parent) not in {"", "."}:
            target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp_ms", "open_time", "open", "high", "low", "close", "volume"])
            for bar in bars:
                writer.writerow(
                    [
                        bar.timestamp_ms,
                        bar.open_time,
                        format(bar.open, "f"),
                        format(bar.high, "f"),
                        format(bar.low, "f"),
                        format(bar.close, "f"),
                        format(bar.volume, "f"),
                    ]
                )
        return len(bars)


def backfill_ohlcv(
    fetch_page: PageFetcher,
    store: CandleStore,
    market: str,
    unit: int,
    hours: int,
    *,
    now_ms: int | None = None,
    page_size: int = 200,
    page_sleep: float = 0.15,
    max_retries: int = 5,
    retry_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> BackfillResult:
    """start 시각까지 분봉을 채웁니다. 이미 있는 구간은 겹치는 순간 멈춥니다."""
    if hours < 1:
        raise ValueError("백필 기간은 1시간 이상이어야 합니다.")
    if unit <= 0 or page_size <= 0:
        raise ValueError("분봉 단위와 페이지 크기는 0보다 커야 합니다.")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    start_ms = now - hours * 3_600_000
    prior_min, prior_max, _prior_count = store.coverage(market, unit)
    max_pages = (hours * 60) // (unit * page_size) + 3
    cursor: str | None = None
    seen_oldest: int | None = None
    pages = 0
    new_rows = 0

    while pages < max_pages:
        bars = _fetch_with_retry(
            fetch_page,
            market,
            unit,
            min(page_size, 200),
            cursor,
            max_retries=max_retries,
            retry_delay=retry_delay,
            sleep=sleep,
        )
        if not bars:
            break
        ordered = sorted(bars, key=lambda bar: bar.timestamp_ms)
        oldest = ordered[0].timestamp_ms
        if seen_oldest is not None and oldest >= seen_oldest:
            logger.warning("백필 페이지가 더 과거로 내려가지 않아 중단합니다. oldest=%s", oldest)
            break
        seen_oldest = oldest
        added = store.upsert(market, unit, ordered)
        new_rows += added
        pages += 1
        logger.info(
            "백필 페이지 %s | %s %s분 | %s ~ %s | 신규 %s",
            pages,
            market,
            unit,
            ordered[0].open_time,
            ordered[-1].open_time,
            added,
        )
        if oldest <= start_ms:
            break
        if prior_max is not None and oldest <= prior_max and prior_min is not None and prior_min <= start_ms:
            break
        cursor = ordered[0].open_time
        if page_sleep > 0:
            sleep(page_sleep)

    stored_min, stored_max, stored = store.coverage(market, unit)
    return BackfillResult(
        pages=pages,
        new_rows=new_rows,
        stored_rows=stored,
        oldest_ms=stored_min,
        newest_ms=stored_max,
    )


def _fetch_with_retry(
    fetch_page: PageFetcher,
    market: str,
    unit: int,
    count: int,
    to: str | None,
    *,
    max_retries: int,
    retry_delay: float,
    sleep: Callable[[float], None],
) -> Sequence[OhlcvBar]:
    delay = retry_delay
    last: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return list(fetch_page(market, unit, count, to))
        except Exception as exc:
            last = exc
            if attempt >= max_retries:
                break
            logger.warning(
                "캔들 백필 실패 (%s/%s, to=%s): %s — %.1f초 후 재시도",
                attempt,
                max_retries,
                to or "now",
                exc,
                delay,
            )
            if delay > 0:
                sleep(delay)
            delay = min(delay * 2, 30)
    if last:
        raise last
    return []


def parse_upbit_candle(row: dict) -> OhlcvBar:
    open_time = str(row.get("candle_date_time_utc") or "")
    if open_time:
        # 체결 시각이 아니라 봉 시작 시각을 키로 써야 진행 중 봉이 중복 저장되지 않습니다.
        parsed = datetime.fromisoformat(open_time).replace(tzinfo=timezone.utc)
        timestamp_ms = int(parsed.timestamp() * 1000)
    else:
        timestamp = row.get("timestamp")
        if timestamp in (None, ""):
            raise ValueError("캔들 시각이 없습니다.")
        timestamp_ms = int(timestamp)
    return OhlcvBar(
        timestamp_ms=timestamp_ms,
        open_time=open_time,
        open=Decimal(str(row["opening_price"])),
        high=Decimal(str(row["high_price"])),
        low=Decimal(str(row["low_price"])),
        close=Decimal(str(row["trade_price"])),
        volume=Decimal(str(row.get("candle_acc_trade_volume") or "0")),
    )
