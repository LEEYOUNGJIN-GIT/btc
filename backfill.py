"""업비트 분봉·일봉을 페이지 단위로 받아 SQLite/CSV 에 쌓습니다.

캔들 REST 는 한 번에 최대 200개입니다. 이미 저장한 시각은 다시 끝까지
당기지 않습니다. 요청 사이에는 쉬고, 오류가 나면 지수 간격으로 재시도합니다.

봇 동적 단가는 최근 분봉을 쓰고, GitHub 적재용 장기 히스토리는 UTC 일봉을
씁니다. `python backfill.py` 기본값은 USDT-BTC·USDT-ETH 최근 5년 일봉입니다.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Sequence

from logger import get_logger

logger = get_logger()

DEFAULT_BACKFILL_MARKETS = ("USDT-BTC", "USDT-ETH")
# SQLite unit 컬럼에 넣는 일봉 표식. 업비트 분봉 unit 과 겹치지 않습니다.
DAY_UNIT = 1440
# GitHub 적재용 권고: 5년 일봉(~3.6k행/마켓). 디스크·PR 모두 작습니다.
DEFAULT_DAILY_DAYS = 1825
MAX_DAILY_DAYS = 3650
DEFAULT_DAILY_CSV = "data/daily/ohlcv.csv"
DEFAULT_DAILY_DB = "data/daily/ohlcv.sqlite"
# 봇 analyzer 룩백(120분+)을 충분히 덮는 GitHub용 분봉. 7일·1분·2마켓 ~3MB.
DEFAULT_MINUTE_HOURS = 168
MAX_MINUTE_HOURS = 24 * 14
DEFAULT_MINUTE_CSV = "data/minute/ohlcv.csv"
DEFAULT_MINUTE_DB = "data/minute/ohlcv.sqlite"

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


def parse_markets(raw: str | Sequence[str] | None, *, default: Sequence[str] = DEFAULT_BACKFILL_MARKETS) -> tuple[str, ...]:
    """콤마/세미콜론으로 나뉜 마켓 목록을 정규화합니다. 비어 있으면 기본값(BTC·ETH)입니다."""
    if raw is None:
        values: list[str] = []
    elif isinstance(raw, str):
        values = [part.strip() for part in raw.replace(";", ",").split(",")]
    else:
        values = []
        for item in raw:
            values.extend(part.strip() for part in str(item).replace(";", ",").split(","))
    markets: list[str] = []
    seen: set[str] = set()
    for value in values:
        market = value.upper()
        if not market or market in seen:
            continue
        parts = market.split("-")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"마켓 코드가 올바르지 않습니다: {value}")
        seen.add(market)
        markets.append(market)
    if not markets:
        return tuple(default)
    return tuple(markets)


def ensure_market(markets: Sequence[str], market: str) -> tuple[str, ...]:
    """거래 마켓이 목록에 없으면 맨 앞에 넣습니다."""
    normalized = parse_markets(markets)
    target = market.strip().upper()
    if not target:
        return normalized
    if target in normalized:
        return normalized
    return (target, *normalized)


def backfill_ohlcv(
    fetch_page: PageFetcher,
    store: CandleStore,
    market: str,
    unit: int,
    hours: int = 0,
    *,
    days: int | None = None,
    now_ms: int | None = None,
    page_size: int = 200,
    page_sleep: float = 0.15,
    max_retries: int = 5,
    retry_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> BackfillResult:
    """start 시각까지 캔들을 채웁니다. 이미 있는 구간은 겹치는 순간 멈춥니다.

    days 를 주면 UTC 일봉(DAY_UNIT)으로 받고, 아니면 분봉(hours)으로 받습니다.
    """
    if page_size <= 0:
        raise ValueError("페이지 크기는 0보다 커야 합니다.")
    if days is not None:
        if not 1 <= days <= MAX_DAILY_DAYS:
            raise ValueError(f"일봉 백필 기간은 1~{MAX_DAILY_DAYS}일입니다.")
        unit = DAY_UNIT
        span_ms = days * 86_400_000
        max_pages = days // page_size + 3
        label = "일"
    else:
        if hours < 1:
            raise ValueError("백필 기간은 1시간 이상이어야 합니다.")
        if unit <= 0:
            raise ValueError("분봉 단위는 0보다 커야 합니다.")
        span_ms = hours * 3_600_000
        max_pages = (hours * 60) // (unit * page_size) + 3
        label = f"{unit}분"
    now = int(time.time() * 1000) if now_ms is None else now_ms
    start_ms = now - span_ms
    prior_min, prior_max, _prior_count = store.coverage(market, unit)
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
            "백필 페이지 %s | %s %s | %s ~ %s | 신규 %s",
            pages,
            market,
            label,
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


def backfill_markets(
    fetch_page: PageFetcher,
    store: CandleStore,
    markets: Sequence[str],
    unit: int,
    hours: int = 0,
    *,
    days: int | None = None,
    now_ms: int | None = None,
    page_size: int = 200,
    page_sleep: float = 0.15,
    max_retries: int = 5,
    retry_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    market_sleep: float = 0.0,
) -> dict[str, BackfillResult]:
    """여러 마켓을 순서대로 백필합니다. 기본 대상은 BTC·ETH 입니다."""
    targets = parse_markets(markets)
    results: dict[str, BackfillResult] = {}
    for index, market in enumerate(targets):
        logger.info("마켓 백필 시작 %s (%s/%s)", market, index + 1, len(targets))
        results[market] = backfill_ohlcv(
            fetch_page,
            store,
            market,
            unit,
            hours,
            days=days,
            now_ms=now_ms,
            page_size=page_size,
            page_sleep=page_sleep,
            max_retries=max_retries,
            retry_delay=retry_delay,
            sleep=sleep,
        )
        if market_sleep > 0 and index + 1 < len(targets):
            sleep(market_sleep)
    return results


def csv_path_for_market(path: str, market: str) -> str:
    """단일 CSV 경로에 마켓 코드를 붙여 파일명을 만듭니다."""
    target = Path(path)
    safe = market.replace("/", "-").replace(" ", "")
    if target.suffix:
        return str(target.with_name(f"{target.stem}_{safe}{target.suffix}"))
    return str(target / f"{safe}.csv")


def export_markets_csv(
    store: CandleStore,
    markets: Sequence[str],
    unit: int,
    path: str,
    start_ms: int | None = None,
) -> Mapping[str, int]:
    targets = parse_markets(markets)
    written: dict[str, int] = {}
    for market in targets:
        out = csv_path_for_market(path, market) if len(targets) > 1 else path
        written[market] = store.export_csv(market, unit, out, start_ms)
    return written


def load_ohlcv_csv(path: str) -> list[OhlcvBar]:
    """GitHub 적재 CSV 를 OhlcvBar 목록으로 읽습니다. open_time 은 UTC 문자열입니다."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"OHLCV CSV 가 없습니다: {path}")
    bars: list[OhlcvBar] = []
    with target.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp_ms", "open_time", "open", "high", "low", "close", "volume"}
        if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"OHLCV CSV 헤더가 올바르지 않습니다: {path}")
        for row in reader:
            bars.append(
                OhlcvBar(
                    timestamp_ms=int(row["timestamp_ms"]),
                    open_time=str(row["open_time"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                )
            )
    return sorted(bars, key=lambda bar: bar.timestamp_ms)


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="업비트 USDT-BTC·USDT-ETH 캔들 백필 (일봉 5년 + 분봉 7일 GitHub 적재)"
    )
    parser.add_argument(
        "--markets",
        default=",".join(DEFAULT_BACKFILL_MARKETS),
        help="콤마로 구분한 마켓. 기본 USDT-BTC,USDT-ETH",
    )
    parser.add_argument(
        "--timeframe",
        choices=("day", "minute", "bundle"),
        default="bundle",
        help="bundle=일봉+분봉(기본), day=UTC 일봉, minute=1분봉",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAILY_DAYS,
        help=f"일봉 기간(UTC). 권고 기본 {DEFAULT_DAILY_DAYS}일(~5년), 최대 {MAX_DAILY_DAYS}",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_MINUTE_HOURS,
        help=f"분봉 기간. 권고 기본 {DEFAULT_MINUTE_HOURS}시간(7일), 최대 {MAX_MINUTE_HOURS}",
    )
    parser.add_argument("--unit", type=int, default=1, help="분봉 단위(timeframe=minute)")
    parser.add_argument("--db", default="", help="SQLite 경로. 비우면 timeframe 기본 경로")
    parser.add_argument(
        "--csv",
        default="",
        help="CSV 경로. 비우면 day/minute 기본 경로. 여러 마켓이면 파일명에 마켓을 붙입니다.",
    )
    parser.add_argument("--page-sleep", type=float, default=0.15, help="페이지 사이 대기(초)")
    parser.add_argument("--market-sleep", type=float, default=0.2, help="마켓 사이 대기(초)")
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    return parser


def _run_backfill(
    *,
    client: object,
    markets: Sequence[str],
    unit: int,
    hours: int,
    days: int | None,
    db_path: str,
    csv_path: str,
    span_ms: int,
    page_sleep: float,
    market_sleep: float,
    max_retries: int,
    retry_delay: float,
) -> dict[str, BackfillResult]:
    with CandleStore(db_path) as store:
        results = backfill_markets(
            client.get_ohlcv_page,  # type: ignore[attr-defined]
            store,
            markets,
            unit,
            hours,
            days=days,
            page_sleep=page_sleep,
            max_retries=max_retries,
            retry_delay=retry_delay,
            market_sleep=market_sleep,
        )
        if csv_path:
            start_ms = int(time.time() * 1000) - span_ms
            written = export_markets_csv(store, markets, unit, csv_path, start_ms)
            for market, rows in written.items():
                out = csv_path_for_market(csv_path, market) if len(markets) > 1 else csv_path
                logger.info("백필 CSV %s행 저장: %s", rows, out)
    for market, result in results.items():
        logger.info(
            "백필 완료 %s | 페이지 %s | 신규 %s | 저장 %s",
            market,
            result.pages,
            result.new_rows,
            result.stored_rows,
        )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    markets = parse_markets(args.markets)

    # exchange_api 가 backfill 을 import 하므로 CLI 진입 시에만 가져옵니다.
    from exchange_api import UpbitREST
    from logger import setup_logger

    setup_logger("backfill.log", "INFO")
    client = UpbitREST(
        access_key="",
        secret_key="",
        http_timeout=10,
        max_retries=args.max_retries,
        retry_delay=args.retry_delay,
        quotation_rps=8.0,
        account_rps=15.0,
        order_rps=6.0,
    )

    jobs: list[tuple[str, int, int, int | None, str, str, int]] = []
    if args.timeframe in {"day", "bundle"}:
        if not 1 <= args.days <= MAX_DAILY_DAYS:
            raise SystemExit(f"days 는 1~{MAX_DAILY_DAYS} 이어야 합니다.")
        jobs.append(
            (
                "day",
                DAY_UNIT,
                0,
                args.days,
                args.db or DEFAULT_DAILY_DB if args.timeframe == "day" else DEFAULT_DAILY_DB,
                (args.csv if args.csv and args.timeframe == "day" else DEFAULT_DAILY_CSV),
                args.days * 86_400_000,
            )
        )
    if args.timeframe in {"minute", "bundle"}:
        if not 1 <= args.hours <= MAX_MINUTE_HOURS:
            raise SystemExit(f"hours 는 1~{MAX_MINUTE_HOURS} 이어야 합니다.")
        if args.unit not in {1, 3, 5, 10, 15, 30, 60, 240}:
            raise SystemExit("unit 이 업비트 분봉 단위가 아닙니다.")
        jobs.append(
            (
                "minute",
                args.unit,
                args.hours,
                None,
                args.db or DEFAULT_MINUTE_DB if args.timeframe == "minute" else DEFAULT_MINUTE_DB,
                (args.csv if args.csv and args.timeframe == "minute" else DEFAULT_MINUTE_CSV),
                args.hours * 3_600_000,
            )
        )

    for _name, unit, hours, days, db_path, csv_path, span_ms in jobs:
        _run_backfill(
            client=client,
            markets=markets,
            unit=unit,
            hours=hours,
            days=days,
            db_path=db_path,
            csv_path=csv_path,
            span_ms=span_ms,
            page_sleep=args.page_sleep,
            market_sleep=args.market_sleep,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
