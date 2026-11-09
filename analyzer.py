"""백필된 분봉으로 ATR, 고저 폭, 단기 박스, 스캘핑 간격을 계산합니다.

그리드 간격은 최근 변동폭을 따르되 기본 0.20%~0.40% 안에 둡니다.
왕복 수수료(기본 0.10%)보다 좁으면 수수료 위로 올립니다. 그 간격이
매수 지정가와 매도 지정가의 거리입니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from backfill import OhlcvBar


@dataclass(frozen=True)
class DynamicLevels:
    support: Decimal
    resistance: Decimal
    grid_rate: Decimal
    atr: Decimal
    atr_rate: Decimal
    mean_range: Decimal
    mean_range_rate: Decimal
    round_trip_fee_rate: Decimal
    sample_size: int
    last_close: Decimal


def analyze(
    bars: Sequence[OhlcvBar],
    *,
    buy_fee_rate: Decimal,
    sell_fee_rate: Decimal,
    atr_period: int = 14,
    range_lookback: int = 120,
    min_grid_rate: Decimal = Decimal("0.002"),
    max_grid_rate: Decimal = Decimal("0.004"),
    support_percentile: Decimal = Decimal("0.10"),
    resistance_percentile: Decimal = Decimal("0.90"),
) -> DynamicLevels:
    if atr_period < 2:
        raise ValueError("ATR 기간은 2 이상이어야 합니다.")
    if not Decimal("0") < min_grid_rate < Decimal("1"):
        raise ValueError("최소 그리드 간격이 올바르지 않습니다.")
    if max_grid_rate <= min_grid_rate:
        raise ValueError("최대 그리드 간격은 최소 간격보다 커야 합니다.")
    ordered = sorted(bars, key=lambda bar: bar.timestamp_ms)
    if len(ordered) < atr_period:
        raise ValueError(f"분석에 필요한 캔들이 부족합니다. 필요 {atr_period}, 보유 {len(ordered)}.")

    window = ordered[-min(len(ordered), range_lookback) :]
    last_close = window[-1].close
    if last_close <= 0:
        raise ValueError("마지막 종가가 0 이하입니다.")

    atr = _atr(ordered, atr_period)
    mean_range = sum((bar.high - bar.low for bar in window), Decimal("0")) / Decimal(len(window))
    atr_rate = atr / last_close
    mean_range_rate = mean_range / last_close
    support = _percentile([bar.low for bar in window], support_percentile)
    resistance = _percentile([bar.high for bar in window], resistance_percentile)
    if resistance <= support:
        support = min(bar.low for bar in window)
        resistance = max(bar.high for bar in window)

    round_trip = buy_fee_rate + sell_fee_rate
    # 간격 자체가 왕복 수수료보다 넓어야 합니다. 0.01%p 는 호가 반올림 여유입니다.
    lower = max(min_grid_rate, round_trip + Decimal("0.0001"))
    upper = max(max_grid_rate, lower)
    raw = max(atr_rate, mean_range_rate)
    if raw < lower:
        grid = lower
    elif raw > upper:
        grid = upper
    else:
        grid = raw

    return DynamicLevels(
        support=support,
        resistance=resistance,
        grid_rate=grid,
        atr=atr,
        atr_rate=atr_rate,
        mean_range=mean_range,
        mean_range_rate=mean_range_rate,
        round_trip_fee_rate=round_trip,
        sample_size=len(window),
        last_close=last_close,
    )


def breakeven_sell(buy_price: Decimal, buy_fee_rate: Decimal, sell_fee_rate: Decimal) -> Decimal:
    """수수료만 회수하는 최소 매도가. 순수익 0."""
    from strategy import align_price

    one = Decimal("1")
    precise = buy_price * (one + buy_fee_rate) / (one - sell_fee_rate)
    additive = buy_price * (one + buy_fee_rate + sell_fee_rate)
    return align_price(max(precise, additive), "ceil")


def grid_sell_price(
    buy_price: Decimal,
    grid_rate: Decimal,
    buy_fee_rate: Decimal,
    sell_fee_rate: Decimal,
) -> Decimal:
    """그리드 간격과 수수료 손익분기 중 더 높은 매도가."""
    from strategy import align_price

    gross = buy_price * (Decimal("1") + grid_rate)
    floor = breakeven_sell(buy_price, buy_fee_rate, sell_fee_rate)
    return align_price(max(gross, floor), "ceil")


def grid_buy_price(
    best_bid: Decimal,
    best_ask: Decimal,
    support: Decimal,
    grid_rate: Decimal,
) -> Decimal | None:
    """지지선과 그리드만큼 아래 호가 중 시장에 더 가까운 매수가."""
    from strategy import align_price

    if best_bid <= 0 or best_ask <= best_bid:
        return None
    offset_price = best_bid * (Decimal("1") - grid_rate)
    band = max(support, offset_price)
    raw = min(best_bid, band)
    if raw <= 0:
        return None
    price = align_price(raw, "floor")
    if price >= best_ask:
        price = align_price(best_bid, "floor")
    if price <= 0 or price >= best_ask:
        return None
    return price


def _atr(bars: Sequence[OhlcvBar], period: int) -> Decimal:
    ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for bar in bars:
        high_low = bar.high - bar.low
        if previous_close is None:
            true_range = high_low
        else:
            true_range = max(high_low, abs(bar.high - previous_close), abs(bar.low - previous_close))
        ranges.append(true_range)
        previous_close = bar.close
    window = ranges[-period:]
    return sum(window, Decimal("0")) / Decimal(len(window))


def closed_bars(bars: Sequence[OhlcvBar], period_ms: int, now_ms: int) -> list[OhlcvBar]:
    """마감된 봉만 남깁니다. 진행 중 봉은 고저가 아직 열려 ATR을 작게 만듭니다."""
    if period_ms <= 0:
        return list(bars)
    return [bar for bar in bars if bar.timestamp_ms + period_ms <= now_ms]


def bars_are_stale(bars: Sequence[OhlcvBar], unit_minutes: int, now_ms: int) -> bool:
    """최신 봉의 시작 시각이 한 주기보다 10분 이상 더 오래되면 시세가 끊긴 것입니다.

    봉 시각은 시작 시각이라, 진행 중인 60분봉은 정상적으로도 수십 분 전 시각을 갖습니다.
    """
    if not bars or unit_minutes <= 0:
        return True
    newest = max(bar.timestamp_ms for bar in bars)
    allowance = unit_minutes * 60_000 + 10 * 60_000
    return now_ms - newest > allowance


@dataclass(frozen=True)
class HistoryStats:
    bar_count: int
    first_open_time: str
    last_open_time: str
    first_close: Decimal
    last_close: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    mean_range_rate: Decimal
    high: Decimal
    low: Decimal


def history_stats(bars: Sequence[OhlcvBar]) -> HistoryStats:
    """장기 OHLCV(일봉·분봉)에서 수익률·낙폭·평균 고저폭을 요약합니다."""
    ordered = sorted(bars, key=lambda bar: bar.timestamp_ms)
    if len(ordered) < 2:
        raise ValueError("히스토리 요약에는 캔들이 2개 이상 필요합니다.")
    first = ordered[0]
    last = ordered[-1]
    if first.close <= 0 or last.close <= 0:
        raise ValueError("종가가 0 이하입니다.")
    peak = ordered[0].close
    max_dd = Decimal("0")
    range_sum = Decimal("0")
    high = ordered[0].high
    low = ordered[0].low
    for bar in ordered:
        high = max(high, bar.high)
        low = min(low, bar.low)
        range_sum += (bar.high - bar.low) / bar.close if bar.close > 0 else Decimal("0")
        if bar.close > peak:
            peak = bar.close
        if peak > 0:
            drawdown = (peak - bar.close) / peak
            if drawdown > max_dd:
                max_dd = drawdown
    return HistoryStats(
        bar_count=len(ordered),
        first_open_time=first.open_time,
        last_open_time=last.open_time,
        first_close=first.close,
        last_close=last.close,
        total_return=(last.close - first.close) / first.close,
        max_drawdown=max_dd,
        mean_range_rate=range_sum / Decimal(len(ordered)),
        high=high,
        low=low,
    )


def close_correlation(left: Sequence[OhlcvBar], right: Sequence[OhlcvBar]) -> Decimal | None:
    """동일 open_time(UTC) 종가 상관계수. 겹치는 봉이 2개 미만이면 None."""
    right_map = {bar.open_time: bar.close for bar in right}
    pairs: list[tuple[Decimal, Decimal]] = []
    for bar in left:
        other = right_map.get(bar.open_time)
        if other is None:
            continue
        pairs.append((bar.close, other))
    if len(pairs) < 2:
        return None
    n = Decimal(len(pairs))
    mean_x = sum((p[0] for p in pairs), Decimal("0")) / n
    mean_y = sum((p[1] for p in pairs), Decimal("0")) / n
    cov = sum(((p[0] - mean_x) * (p[1] - mean_y) for p in pairs), Decimal("0"))
    var_x = sum(((p[0] - mean_x) ** 2 for p in pairs), Decimal("0"))
    var_y = sum(((p[1] - mean_y) ** 2 for p in pairs), Decimal("0"))
    if var_x <= 0 or var_y <= 0:
        return None
    # Decimal sqrt
    return cov / (var_x * var_y).sqrt()


def _percentile(values: Sequence[Decimal], quantile: Decimal) -> Decimal:
    if not values:
        raise ValueError("백분위 계산에 값이 없습니다.")
    if not Decimal("0") <= quantile <= 1:
        raise ValueError("백분위는 0과 1 사이여야 합니다.")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = quantile * Decimal(len(ordered) - 1)
    low_index = int(rank)
    high_index = min(low_index + 1, len(ordered) - 1)
    fraction = rank - Decimal(low_index)
    return ordered[low_index] + (ordered[high_index] - ordered[low_index]) * fraction
