#!/usr/bin/env python3
"""GitHub 적재 OHLCV(분봉·일봉)를 analyzer·scalp_zone 으로 요약합니다.

- data/minute/: 봇과 동일한 ATR·박스·그리드(analyze)
- data/daily/: 동일 동적 단가 지표 + 장기 수익률·낙폭·BTC/ETH 상관
- 둘을 합친 스캘핑 구간(scalp_zone.compute_scalping_zone)
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from analyzer import DynamicLevels, analyze, close_correlation, history_stats
from backfill import DEFAULT_BACKFILL_MARKETS, load_ohlcv_csv
from logger import get_logger, setup_logger
from scalp_zone import compute_scalping_zone

logger = get_logger()

DEFAULT_MINUTE_DIR = Path("data/minute")
DEFAULT_DAILY_DIR = Path("data/daily")
DEFAULT_REPORT_JSON = Path("data/reports/analysis.json")
DEFAULT_REPORT_MD = Path("data/reports/analysis.md")


def _dec(value: Decimal) -> str:
    return format(value, "f")


def _pct(value: Decimal) -> str:
    return f"{float(value * 100):.4f}%"


def _levels_dict(levels: DynamicLevels) -> dict[str, Any]:
    return {
        "support": _dec(levels.support),
        "resistance": _dec(levels.resistance),
        "grid_rate": _dec(levels.grid_rate),
        "grid_rate_pct": _pct(levels.grid_rate),
        "atr": _dec(levels.atr),
        "atr_rate_pct": _pct(levels.atr_rate),
        "mean_range_rate_pct": _pct(levels.mean_range_rate),
        "round_trip_fee_rate_pct": _pct(levels.round_trip_fee_rate),
        "sample_size": levels.sample_size,
        "last_close": _dec(levels.last_close),
    }


def analyze_csv(
    path: Path,
    *,
    buy_fee_rate: Decimal,
    sell_fee_rate: Decimal,
    atr_period: int,
    range_lookback: int,
    min_grid_rate: Decimal,
    max_grid_rate: Decimal,
    period_ms: int | None = None,
) -> dict[str, Any]:
    bars = load_ohlcv_csv(str(path))
    levels = analyze(
        bars,
        period_ms=period_ms,
        buy_fee_rate=buy_fee_rate,
        sell_fee_rate=sell_fee_rate,
        atr_period=atr_period,
        range_lookback=range_lookback,
        min_grid_rate=min_grid_rate,
        max_grid_rate=max_grid_rate,
    )
    hist = history_stats(bars)
    return {
        "path": str(path).replace("\\", "/"),
        "bar_count": hist.bar_count,
        "first_open_time_utc": hist.first_open_time,
        "last_open_time_utc": hist.last_open_time,
        "dynamic_levels": _levels_dict(levels),
        "history": {
            "total_return_pct": _pct(hist.total_return),
            "max_drawdown_pct": _pct(hist.max_drawdown),
            "mean_range_rate_pct": _pct(hist.mean_range_rate),
            "first_close": _dec(hist.first_close),
            "last_close": _dec(hist.last_close),
            "high": _dec(hist.high),
            "low": _dec(hist.low),
        },
    }


def _market_from_name(path: Path) -> str | None:
    stem = path.stem
    for market in DEFAULT_BACKFILL_MARKETS:
        if stem.endswith(market) or market in stem:
            return market
    return None


def _collect_csvs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.csv") if path.is_file())


def _zone_dict(zone: Any) -> dict[str, Any]:
    return {
        "market": zone.market,
        "bias": zone.bias,
        "actionable": zone.actionable,
        "reason": zone.reason,
        "zone_low": _dec(zone.zone_low),
        "zone_high": _dec(zone.zone_high),
        "target_rate_pct": _pct(zone.target_rate),
        "stop_rate_pct": _pct(zone.stop_rate),
        "reference_buy": _dec(zone.reference_buy),
        "target_sell": _dec(zone.target_sell),
        "stop_price": _dec(zone.stop_price),
        "daily_position_pct": _pct(zone.daily_position),
        "daily_sma": _dec(zone.daily_sma),
        "minute_levels": _levels_dict(zone.minute),
        "daily_levels": _levels_dict(zone.daily),
        "blended_levels": _levels_dict(zone.blended),
    }


def build_report(
    *,
    minute_dir: Path,
    daily_dir: Path,
    buy_fee_rate: Decimal,
    sell_fee_rate: Decimal,
    atr_period: int,
    range_lookback: int,
    min_grid_rate: Decimal,
    max_grid_rate: Decimal,
    entry_zone: Decimal = Decimal("0.40"),
    breakdown_buffer: Decimal = Decimal("0.002"),
    stop_loss_rate: Decimal = Decimal("0.005"),
    daily_sma_period: int = 20,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "minute": [],
        "daily": [],
        "scalping_zones": [],
        "daily_btc_eth_close_correlation": None,
    }
    minute_bars: dict[str, list] = {}
    for path in _collect_csvs(minute_dir):
        try:
            item = analyze_csv(
                path,
                buy_fee_rate=buy_fee_rate,
                sell_fee_rate=sell_fee_rate,
                atr_period=atr_period,
                range_lookback=range_lookback,
                min_grid_rate=min_grid_rate,
                max_grid_rate=max_grid_rate,
                period_ms=60_000,
            )
            market = _market_from_name(path)
            item["market"] = market
            report["minute"].append(item)
            if market:
                minute_bars[market] = load_ohlcv_csv(str(path))
            logger.info(
                "분봉 분석 %s | grid %s | ATR %s",
                path.name,
                item["dynamic_levels"]["grid_rate_pct"],
                item["dynamic_levels"]["atr_rate_pct"],
            )
        except Exception:
            logger.exception("분봉 분석 실패: %s", path)

    daily_bars: dict[str, list] = {}
    for path in _collect_csvs(daily_dir):
        try:
            item = analyze_csv(
                path,
                buy_fee_rate=buy_fee_rate,
                sell_fee_rate=sell_fee_rate,
                atr_period=atr_period,
                range_lookback=range_lookback,
                min_grid_rate=min_grid_rate,
                max_grid_rate=max_grid_rate,
            )
            market = _market_from_name(path)
            item["market"] = market
            report["daily"].append(item)
            if market:
                daily_bars[market] = load_ohlcv_csv(str(path))
            logger.info(
                "일봉 분석 %s | return %s | MDD %s | grid %s",
                path.name,
                item["history"]["total_return_pct"],
                item["history"]["max_drawdown_pct"],
                item["dynamic_levels"]["grid_rate_pct"],
            )
        except Exception:
            logger.exception("일봉 분석 실패: %s", path)

    for market in sorted(set(minute_bars) & set(daily_bars)):
        try:
            zone = compute_scalping_zone(
                minute_bars[market],
                daily_bars[market],
                market=market,
                buy_fee_rate=buy_fee_rate,
                sell_fee_rate=sell_fee_rate,
                atr_period=atr_period,
                range_lookback=range_lookback,
                min_grid_rate=min_grid_rate,
                max_grid_rate=max_grid_rate,
                entry_zone=entry_zone,
                breakdown_buffer=breakdown_buffer,
                stop_loss_rate=stop_loss_rate,
                daily_sma_period=daily_sma_period,
            )
            payload = _zone_dict(zone)
            report["scalping_zones"].append(payload)
            logger.info(
                "스캘핑 구간 %s | bias=%s actionable=%s | %s~%s | 목표 %s | %s",
                market,
                zone.bias,
                zone.actionable,
                payload["zone_low"],
                payload["zone_high"],
                payload["target_rate_pct"],
                zone.reason,
            )
        except Exception:
            logger.exception("스캘핑 구간 산출 실패: %s", market)

    btc = daily_bars.get("USDT-BTC")
    eth = daily_bars.get("USDT-ETH")
    if btc and eth:
        corr = close_correlation(btc, eth)
        if corr is not None:
            report["daily_btc_eth_close_correlation"] = f"{float(corr):.6f}"
            logger.info("일봉 BTC-ETH 종가 상관 %s", report["daily_btc_eth_close_correlation"])
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# OHLCV 백필 분석",
        "",
        "분봉은 봇 `analyzer.analyze` 와 동일한 동적 단가 지표입니다.",
        "일봉은 같은 지표에 더해 장기 수익률·최대낙폭·BTC/ETH 상관을 붙입니다.",
        "스캘핑 구간은 분봉·일봉을 동시에 검토한 `scalp_zone` 결과입니다.",
        "",
    ]
    corr = report.get("daily_btc_eth_close_correlation")
    if corr is not None:
        lines.extend([f"- 일봉 BTC↔ETH 종가 상관: `{corr}`", ""])

    zones = report.get("scalping_zones") or []
    lines.append("## 스캘핑 구간 (분봉+일봉)")
    lines.append("")
    if not zones:
        lines.append("_산출 없음_")
        lines.append("")
    else:
        for zone in zones:
            lines.append(f"### {zone['market']}")
            lines.append("")
            lines.append(
                f"- bias `{zone['bias']}` / actionable `{zone['actionable']}` — {zone['reason']}"
            )
            lines.append(
                f"- 진입 구간: `{zone['zone_low']}` ~ `{zone['zone_high']}` "
                f"(목표 `{zone['target_rate_pct']}`, 손절 `{zone['stop_rate_pct']}`)"
            )
            lines.append(
                f"- 참고 매수 `{zone['reference_buy']}` → 목표가 `{zone['target_sell']}` / "
                f"손절 `{zone['stop_price']}` / 일봉위치 `{zone['daily_position_pct']}`"
            )
            lines.append("")

    def section(title: str, rows: list[dict[str, Any]]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not rows:
            lines.append("_파일 없음_")
            lines.append("")
            return
        for item in rows:
            levels = item["dynamic_levels"]
            hist = item["history"]
            lines.append(f"### {item.get('market') or item['path']}")
            lines.append("")
            lines.append(f"- 파일: `{item['path']}`")
            lines.append(f"- 구간(UTC): `{item['first_open_time_utc']}` ~ `{item['last_open_time_utc']}`")
            lines.append(f"- 봉 수: `{item['bar_count']}`")
            lines.append(
                f"- 동적 단가: support `{levels['support']}`, resistance `{levels['resistance']}`, "
                f"grid `{levels['grid_rate_pct']}`, ATR `{levels['atr_rate_pct']}`"
            )
            lines.append(
                f"- 히스토리: return `{hist['total_return_pct']}`, MDD `{hist['max_drawdown_pct']}`, "
                f"mean range `{hist['mean_range_rate_pct']}`"
            )
            lines.append("")

    section("분봉 (data/minute)", report.get("minute") or [])
    section("일봉 (data/daily)", report.get("daily") or [])
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="분봉·일봉 GitHub CSV 분석 리포트")
    parser.add_argument("--minute-dir", default=str(DEFAULT_MINUTE_DIR))
    parser.add_argument("--daily-dir", default=str(DEFAULT_DAILY_DIR))
    parser.add_argument("--json-out", default=str(DEFAULT_REPORT_JSON))
    parser.add_argument("--md-out", default=str(DEFAULT_REPORT_MD))
    parser.add_argument("--buy-fee", default="0.0005")
    parser.add_argument("--sell-fee", default="0.0005")
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--range-lookback", type=int, default=120)
    parser.add_argument("--min-grid", default="0.002")
    parser.add_argument("--max-grid", default="0.004")
    parser.add_argument("--entry-zone", default="0.40")
    parser.add_argument("--breakdown-buffer", default="0.002")
    parser.add_argument("--stop-loss", default="0.005")
    parser.add_argument("--daily-sma-period", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_logger("report.log", "INFO")
    report = build_report(
        minute_dir=Path(args.minute_dir),
        daily_dir=Path(args.daily_dir),
        buy_fee_rate=Decimal(args.buy_fee),
        sell_fee_rate=Decimal(args.sell_fee),
        atr_period=args.atr_period,
        range_lookback=args.range_lookback,
        min_grid_rate=Decimal(args.min_grid),
        max_grid_rate=Decimal(args.max_grid),
        entry_zone=Decimal(args.entry_zone),
        breakdown_buffer=Decimal(args.breakdown_buffer),
        stop_loss_rate=Decimal(args.stop_loss),
        daily_sma_period=args.daily_sma_period,
    )
    json_path = Path(args.json_out)
    md_path = Path(args.md_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    logger.info("리포트 저장 %s , %s", json_path, md_path)
    if not report["minute"] and not report["daily"]:
        logger.error("분석할 CSV 가 없습니다. backfill 후 다시 실행하세요.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
