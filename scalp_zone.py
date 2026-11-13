"""분봉·일봉을 같이 보고 스캘핑 진입 구간을 산출합니다.

분봉: ATR·박스·그리드(봇 analyzer 와 동일)로 단기 매수 밴드/목표 간격을 잡습니다.
일봉: 같은 지표 + SMA·박스 위치로 추세 필터(허용/중립/회피)를 겁니다.
두 결과를 합쳐 zone_low~zone_high, target_rate, stop_rate, actionable 을 냅니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from analyzer import DynamicLevels, analyze, grid_sell_price
from backfill import OhlcvBar
from strategy import align_price


@dataclass(frozen=True)
class ScalpingZone:
    market: str
    bias: str
    actionable: bool
    reason: str
    zone_low: Decimal
    zone_high: Decimal
    target_rate: Decimal
    stop_rate: Decimal
    target_sell: Decimal
    stop_price: Decimal
    reference_buy: Decimal
    minute: DynamicLevels
    daily: DynamicLevels
    daily_position: Decimal
    daily_sma: Decimal
    blended: DynamicLevels


def _sma(bars: Sequence[OhlcvBar], period: int) -> Decimal:
    window = list(bars)[-period:]
    if not window:
        raise ValueError("SMA 계산에 캔들이 없습니다.")
    return sum((bar.close for bar in window), Decimal("0")) / Decimal(len(window))


def _clip(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def compute_scalping_zone(
    minute_bars: Sequence[OhlcvBar],
    daily_bars: Sequence[OhlcvBar],
    *,
    market: str = "",
    buy_fee_rate: Decimal = Decimal("0.0005"),
    sell_fee_rate: Decimal = Decimal("0.0005"),
    atr_period: int = 14,
    range_lookback: int = 120,
    min_grid_rate: Decimal = Decimal("0.002"),
    max_grid_rate: Decimal = Decimal("0.004"),
    support_percentile: Decimal = Decimal("0.10"),
    resistance_percentile: Decimal = Decimal("0.90"),
    entry_zone: Decimal = Decimal("0.40"),
    breakdown_buffer: Decimal = Decimal("0.002"),
    stop_loss_rate: Decimal = Decimal("0.005"),
    daily_sma_period: int = 20,
    minute_period_ms: int = 60_000,
) -> ScalpingZone:
    """분봉·일봉 기술적 지표로 스캘핑 허용 구간을 만듭니다."""
    if not Decimal("0") < entry_zone <= 1:
        raise ValueError("entry_zone 은 0 초과 1 이하여야 합니다.")
    minute = analyze(
        minute_bars,
        buy_fee_rate=buy_fee_rate,
        sell_fee_rate=sell_fee_rate,
        atr_period=atr_period,
        range_lookback=range_lookback,
        min_grid_rate=min_grid_rate,
        max_grid_rate=max_grid_rate,
        support_percentile=support_percentile,
        resistance_percentile=resistance_percentile,
        period_ms=minute_period_ms,
    )
    daily = analyze(
        daily_bars,
        buy_fee_rate=buy_fee_rate,
        sell_fee_rate=sell_fee_rate,
        atr_period=atr_period,
        range_lookback=min(range_lookback, len(daily_bars)),
        min_grid_rate=min_grid_rate,
        max_grid_rate=max_grid_rate,
        support_percentile=support_percentile,
        resistance_percentile=resistance_percentile,
    )
    sma = _sma(daily_bars, min(daily_sma_period, len(daily_bars)))
    daily_span = daily.resistance - daily.support
    if daily_span <= 0:
        daily_pos = Decimal("0.5")
    else:
        daily_pos = (daily.last_close - daily.support) / daily_span

    if daily.last_close < daily.support * (Decimal("1") - breakdown_buffer):
        bias, actionable, reason = "avoid", False, "일봉 지지 이탈"
    elif daily_pos >= Decimal("0.85") and daily.last_close < sma:
        bias, actionable, reason = "avoid", False, "일봉 상단인데 이평 아래(약세 되돌림 위험)"
    elif daily_pos <= entry_zone and daily.last_close >= sma:
        bias, actionable, reason = "long_bias", True, "일봉 하단+이평 위: 분봉 스캘핑 유리"
    elif daily_pos <= entry_zone:
        bias, actionable, reason = "long_bias", True, "일봉 하단 구간: 분봉 저가 매수 허용"
    elif daily_pos <= Decimal("0.55") and daily.last_close >= sma:
        bias, actionable, reason = "neutral", True, "일봉 중하단·이평 위: 축소 진입"
    else:
        bias, actionable, reason = "neutral", False, "일봉 중상단: 신규 스캘핑 보류"

    minute_span = minute.resistance - minute.support
    if minute_span <= 0:
        raise ValueError("분봉 박스가 비어 있습니다.")
    zone_low = minute.support
    zone_high = minute.support + minute_span * entry_zone
    floor = daily.support * (Decimal("1") - breakdown_buffer)
    if zone_low < floor < zone_high:
        zone_low = floor
    if zone_high <= zone_low:
        zone_high = zone_low * (Decimal("1") + max(min_grid_rate, Decimal("0.001")))

    blended_raw = max(minute.grid_rate, min(daily.atr_rate / Decimal("8"), max_grid_rate))
    target_rate = _clip(blended_raw, min_grid_rate, max_grid_rate)
    fee_floor = buy_fee_rate + sell_fee_rate + Decimal("0.0001")
    if target_rate < fee_floor:
        target_rate = min(max(fee_floor, min_grid_rate), max_grid_rate)

    if bias == "neutral" and actionable:
        target_rate = max(fee_floor, min(target_rate, (min_grid_rate + max_grid_rate) / 2))

    ref_buy = align_price((zone_low + zone_high) / 2, "floor")
    if ref_buy <= 0:
        ref_buy = align_price(minute.last_close, "floor")
    target_sell = grid_sell_price(ref_buy, target_rate, buy_fee_rate, sell_fee_rate)
    stop = align_price(ref_buy * (Decimal("1") - stop_loss_rate), "floor")

    blended = DynamicLevels(
        support=zone_low,
        resistance=minute.resistance,
        grid_rate=target_rate,
        atr=minute.atr,
        atr_rate=minute.atr_rate,
        mean_range=minute.mean_range,
        mean_range_rate=minute.mean_range_rate,
        round_trip_fee_rate=buy_fee_rate + sell_fee_rate,
        sample_size=minute.sample_size,
        last_close=minute.last_close,
    )
    return ScalpingZone(
        market=market,
        bias=bias,
        actionable=actionable,
        reason=reason,
        zone_low=zone_low,
        zone_high=zone_high,
        target_rate=target_rate,
        stop_rate=stop_loss_rate,
        target_sell=target_sell,
        stop_price=stop,
        reference_buy=ref_buy,
        minute=minute,
        daily=daily,
        daily_position=daily_pos,
        daily_sma=sma,
        blended=blended,
    )
