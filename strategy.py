"""박스권 진입, 수수료 반영 손익분기, 손절 가격을 계산합니다.

매도 지정가는 아래 두 기준 중 더 높은 가격 이상으로만 잡습니다.
1) 업비트 수수료 구조: 매수 대금에 매수 수수료를 더하고, 매도 대금에서 매도 수수료를 뺀 뒤
   목표 순수익률이 남도록 하는 가격
2) 사용자가 명시한 가산 기준: 매수가 * (1 + 매수수수료 + 매도수수료 + 목표수익률)
   수수료가 각 0.05%이면 왕복 0.10% + 목표수익률입니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Sequence

from analyzer import DynamicLevels, grid_buy_price, grid_sell_price


# 2024-08-19 이후 USDT 마켓 호가 단위.
# https://docs.upbit.com/kr/docs/usdt-market-info
_USDT_TICKS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("10"), Decimal("0.01")),
    (Decimal("1"), Decimal("0.001")),
    (Decimal("0.1"), Decimal("0.0001")),
    (Decimal("0.01"), Decimal("0.00001")),
    (Decimal("0.001"), Decimal("0.000001")),
    (Decimal("0.0001"), Decimal("0.0000001")),
)
_MIN_USDT_TICK = Decimal("0.00000001")


def usdt_tick(price: Decimal) -> Decimal:
    if price <= 0:
        raise ValueError("가격은 0보다 커야 합니다.")
    for threshold, tick in _USDT_TICKS:
        if price >= threshold:
            return tick
    return _MIN_USDT_TICK


def align_price(price: Decimal, mode: str) -> Decimal:
    """호가 단위에 맞춥니다. 매수는 floor, 이익 매도는 ceil 을 써서 기준을 깎지 않습니다."""
    if price <= 0:
        raise ValueError("가격은 0보다 커야 합니다.")
    rounding = {"floor": ROUND_DOWN, "ceil": ROUND_UP, "round": ROUND_HALF_UP}[mode]
    tick = usdt_tick(price)
    aligned = (price / tick).to_integral_value(rounding) * tick
    if aligned <= 0:
        aligned = tick
    crossed = usdt_tick(aligned)
    if crossed != tick:
        aligned = (aligned / crossed).to_integral_value(rounding) * crossed
    return aligned


def floor_to_precision(value: Decimal, precision: int) -> Decimal:
    quantum = Decimal("1").scaleb(-precision)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def to_plain_str(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class StrategyParams:
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    target_profit_rate: Decimal
    stop_loss_rate: Decimal
    buy_offset_rate: Decimal
    entry_zone: Decimal
    breakdown_buffer: Decimal
    min_order_quote: Decimal
    balance_use_ratio: Decimal
    quote_reserve: Decimal
    volume_precision: int

    def validate(self) -> None:
        if self.buy_fee_rate < 0 or not Decimal("0") <= self.sell_fee_rate < 1:
            raise ValueError("수수료율이 올바르지 않습니다.")
        if self.target_profit_rate <= 0:
            raise ValueError("목표 수익률은 0보다 커야 합니다.")
        if not Decimal("0") < self.stop_loss_rate < 1:
            raise ValueError("손절 비율은 0과 1 사이여야 합니다.")
        if self.buy_offset_rate < 0:
            raise ValueError("매수 이격은 0 이상이어야 합니다.")
        if not Decimal("0") < self.entry_zone <= 1:
            raise ValueError("ENTRY_ZONE 은 0 초과 1 이하여야 합니다.")
        if self.breakdown_buffer < 0:
            raise ValueError("BREAKDOWN_BUFFER 는 0 이상이어야 합니다.")
        if self.min_order_quote <= 0:
            raise ValueError("최소 주문금액은 0보다 커야 합니다.")
        if not Decimal("0") < self.balance_use_ratio <= 1:
            raise ValueError("BALANCE_USE_RATIO 는 0 초과 1 이하여야 합니다.")
        if self.quote_reserve < 0:
            raise ValueError("QUOTE_RESERVE 는 0 이상이어야 합니다.")
        if not 0 <= self.volume_precision <= 8:
            raise ValueError("수량은 소수점 8자리까지만 사용합니다.")


@dataclass(frozen=True)
class Candle:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    trade_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    box_low: Decimal
    box_high: Decimal


@dataclass(frozen=True)
class BuyPlan:
    price: Decimal
    volume: Decimal
    notional: Decimal
    fee_reserve: Decimal
    note: str


@dataclass(frozen=True)
class Decision:
    plan: BuyPlan | None
    skip_reason: str | None = None


def box_from_candles(candles: Sequence[Candle]) -> tuple[Decimal, Decimal]:
    lows = [candle.low for candle in candles if candle.low > 0]
    highs = [candle.high for candle in candles if candle.high > 0]
    if not lows or not highs:
        raise ValueError("박스 계산에 쓸 캔들이 없습니다.")
    return min(lows), max(highs)


class Strategy:
    def __init__(self, params: StrategyParams):
        params.validate()
        self.params = params
        self.levels: DynamicLevels | None = None

    def update_levels(self, levels: DynamicLevels | None) -> None:
        """백필 분석 결과로 박스와 그리드 간격을 바꿉니다. None 이면 고정 퍼센트입니다."""
        self.levels = levels

    def min_sell_price(self, buy_price: Decimal) -> Decimal:
        """수수료를 상회하는 최소 매도가.

        동적 그리드가 있으면 그 간격(기본 0.20%~0.40%)과 수수료 손익분기 중 높은 쪽입니다.
        그리드가 없으면 기존처럼 목표 순수익률을 수수료 위에 더합니다.
        """
        if buy_price <= 0:
            raise ValueError("매수가가 올바르지 않습니다.")
        if self.levels is not None:
            return grid_sell_price(
                buy_price,
                self.levels.grid_rate,
                self.params.buy_fee_rate,
                self.params.sell_fee_rate,
            )
        params = self.params
        one = Decimal("1")
        precise = (
            buy_price
            * (one + params.buy_fee_rate)
            * (one + params.target_profit_rate)
            / (one - params.sell_fee_rate)
        )
        additive = buy_price * (
            one + params.buy_fee_rate + params.sell_fee_rate + params.target_profit_rate
        )
        return align_price(max(precise, additive), "ceil")

    def stop_price(self, buy_price: Decimal) -> Decimal:
        raw = buy_price * (Decimal("1") - self.params.stop_loss_rate)
        return align_price(raw, "floor")

    def _entry_frame(self, snapshot: MarketSnapshot) -> tuple[Decimal, Decimal, Decimal]:
        if self.levels is not None and self.levels.resistance > self.levels.support > 0:
            return self.levels.support, self.levels.resistance, self.levels.grid_rate
        return snapshot.box_low, snapshot.box_high, self.params.buy_offset_rate

    def plan_buy(self, snapshot: MarketSnapshot, quote_balance: Decimal) -> Decision:
        """하단 구간에서만, 가용 USDT 로 살 수 있는 지정가 매수 계획을 만듭니다."""
        if snapshot.trade_price <= 0 or snapshot.best_bid <= 0 or snapshot.best_ask <= 0:
            return Decision(None, "현재가 또는 호가가 없습니다.")
        if snapshot.best_ask <= snapshot.best_bid:
            return Decision(None, "호가가 교차되어 매수를 보류합니다.")
        box_low, box_high, offset_rate = self._entry_frame(snapshot)
        if box_high <= box_low:
            return Decision(None, "박스 범위가 없어 매수를 보류합니다.")

        params = self.params
        floor = box_low * (Decimal("1") - params.breakdown_buffer)
        if snapshot.trade_price < floor:
            return Decision(None, "박스 하단을 이탈해 매수를 보류합니다.")

        span = box_high - box_low
        location = Decimal("1") if span <= 0 else (snapshot.trade_price - box_low) / span
        if location > params.entry_zone:
            return Decision(
                None,
                "박스 상단 쪽이라 매수를 보류합니다 "
                f"(위치 {(location * 100):.2f}%, 허용 {(params.entry_zone * 100):.0f}%).",
            )

        if self.levels is not None:
            price = grid_buy_price(snapshot.best_bid, snapshot.best_ask, box_low, offset_rate)
            if price is None:
                return Decision(None, "테이커로 호가를 잠식하지 않도록 매수가를 만들지 못했습니다.")
        else:
            offset_price = snapshot.best_bid * (Decimal("1") - offset_rate)
            # 지지(박스 저가)와 '호가 아래 이격' 중 시장에 더 가까운 가격을 하단 매수가로 씁니다.
            band = max(box_low, offset_price)
            raw = min(snapshot.best_bid, band)
            if raw <= 0:
                return Decision(None, "계산된 매수가가 0 이하입니다.")
            price = align_price(raw, "floor")
            if price >= snapshot.best_ask:
                price = align_price(snapshot.best_bid, "floor")
            if price <= 0 or price >= snapshot.best_ask:
                return Decision(None, "테이커로 호가를 잠식하지 않도록 매수가를 만들지 못했습니다.")

        sized = self._size_buy(quote_balance, price)
        if sized is None:
            return Decision(None, "주문 가능 USDT 가 최소 주문금액보다 작습니다.")
        volume, notional, fee_reserve = sized
        grid_note = ""
        if self.levels is not None:
            grid_note = f", 그리드 {(offset_rate * 100):.3f}%"
        note = (
            f"박스 {to_plain_str(box_low)}~{to_plain_str(box_high)}, "
            f"위치 {(location * 100):.2f}%, 매수 {to_plain_str(price)}{grid_note}"
        )
        return Decision(
            BuyPlan(
                price=price,
                volume=volume,
                notional=notional,
                fee_reserve=fee_reserve,
                note=note,
            )
        )

    def _size_buy(self, quote_balance: Decimal, price: Decimal) -> tuple[Decimal, Decimal, Decimal] | None:
        """수수료를 견적자산에서 떼는 전제로, 잔고를 넘지 않게 수량을 내림합니다."""
        params = self.params
        budget = quote_balance * params.balance_use_ratio - params.quote_reserve
        if budget <= 0:
            return None
        unit_cost = price * (Decimal("1") + params.buy_fee_rate)
        volume = floor_to_precision(budget / unit_cost, params.volume_precision)
        if volume <= 0:
            return None
        notional = price * volume
        fee = notional * params.buy_fee_rate
        if notional + fee > quote_balance:
            step = Decimal("1").scaleb(-params.volume_precision)
            volume = floor_to_precision(volume - step, params.volume_precision)
            if volume <= 0:
                return None
            notional = price * volume
            fee = notional * params.buy_fee_rate
        if notional < params.min_order_quote or notional + fee > quote_balance:
            return None
        return volume, notional, fee

    def next_sell_price(
        self,
        current_price: Decimal,
        best_ask: Decimal,
        min_sell: Decimal,
    ) -> Decimal | None:
        """미체결 매도를 호가에 더 가깝게 옮기되, 최소 이익 가격 아래로는 내리지 않습니다."""
        if current_price < min_sell:
            return align_price(min_sell, "ceil")
        if best_ask < min_sell:
            return None
        target = align_price(max(best_ask, min_sell), "ceil")
        if target < min_sell:
            target = align_price(min_sell, "ceil")
        if target >= current_price:
            return None
        if target == current_price:
            return None
        return target

    def should_stop(self, trade_price: Decimal, stop: Decimal) -> bool:
        # 손절가 0 은 '손절 없음'(평단 불명)입니다. 현재가 0 은 시세 오류로 보고 청산하지 않습니다.
        return stop > 0 and trade_price > 0 and trade_price <= stop


def net_pnl(
    buy_notional: Decimal,
    buy_fee: Decimal,
    sell_funds: Decimal,
    sell_fee: Decimal,
) -> tuple[Decimal, Decimal]:
    """순손익과 매수원가 대비 수익률을 반환합니다."""
    cost = buy_notional + buy_fee
    proceeds = sell_funds - sell_fee
    net = proceeds - cost
    ratio = Decimal("0") if cost <= 0 else net / cost
    return net, ratio
