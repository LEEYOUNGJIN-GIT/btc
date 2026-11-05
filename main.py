#!/usr/bin/env python3
"""USDT 현물 왕복 스캘핑 루프.

FLAT -> 박스 하단에서 USDT 전액 지정가 매수 -> 체결되면 수수료와 목표 수익을 반영한
지정가 매도 -> 체결되면 다시 현금. 숏은 하지 않습니다.
손절은 거래소 조건부 주문이 아니라, 프로세스가 살아 있을 때 현재가를 보고 실행합니다.
"""

from __future__ import annotations

import json
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from config import AppConfig, load_config
from exchange_api import (
    NullFeed,
    Order,
    PriceFeed,
    UpbitAPIError,
    WebsocketTicker,
    build_exchange,
)
from logger import get_logger, setup_logger
from strategy import (
    MarketSnapshot,
    Strategy,
    align_price,
    box_from_candles,
    floor_to_precision,
    net_pnl,
    to_plain_str,
)

logger = get_logger()


@dataclass
class Position:
    cycle_id: str
    buy_price: Decimal
    volume: Decimal
    buy_notional: Decimal
    buy_fee: Decimal
    stop_price: Decimal
    min_sell_price: Decimal
    sold_volume: Decimal = Decimal("0")
    sold_funds: Decimal = Decimal("0")
    sold_fee: Decimal = Decimal("0")
    exit_attempts: int = 0
    estimated: bool = False


class RoundTripBot:
    def __init__(self, cfg: AppConfig, exchange: object, strategy: Strategy, feed: PriceFeed):
        self.cfg = cfg
        self.exchange = exchange
        self.strategy = strategy
        self.feed = feed
        self.phase = "FLAT"
        self.order_uuid: str | None = None
        self.order_placed_at = 0.0
        self.position: Position | None = None
        self.cooldown_until = 0.0
        self.realized = Decimal("0")
        self._pnl_date = datetime.now(timezone.utc).date()
        self.completed_cycles = 0
        self._stop = False
        self._user_cancel = False
        self._seen_sells: set[str] = set()
        self._candles: list = []
        self._candle_at = 0.0
        self._last_price = Decimal("0")
        self._last_skip = ""
        self._last_skip_at = 0.0
        self._steps = 0
        self._halt_logged = False
        self._started = time.monotonic()

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        self._countdown()
        if self._stop:
            return
        self._boot()
        while not self._stop:
            if self.cfg.max_runtime_sec > 0 and time.monotonic() - self._started >= self.cfg.max_runtime_sec:
                logger.info("MAX_RUNTIME_SEC 에 도달해 종료합니다.")
                break
            try:
                self.step()
            except Exception:
                logger.exception("루프 오류. 프로세스는 유지하고 다음 주기에 재시도합니다.")
                self._sleep(min(max(self.cfg.retry_delay_sec, 1.0), 10.0))
                continue
            self._sleep(self.cfg.loop_interval_sec)
        self.shutdown()

    def step(self) -> None:
        self.exchange.begin_step()
        self._roll_daily()
        snapshot = self._snapshot()
        self._steps += 1
        if self._steps % self.cfg.health_every == 0:
            self._log_health(snapshot)
        if self.phase == "HALT":
            if not self._halt_logged:
                logger.error("봇이 중지 상태입니다. 잔고와 미체결 주문을 확인한 뒤 다시 실행하세요.")
                self._halt_logged = True
            return
        if self.phase == "FLAT":
            self._step_flat(snapshot)
        elif self.phase == "BUY_WAIT":
            self._step_buy(snapshot)
        elif self.phase == "HOLDING":
            self._step_holding(snapshot)
        elif self.phase == "SELL_WAIT":
            self._step_sell(snapshot)
        else:
            logger.error("알 수 없는 상태 %s. 중지합니다.", self.phase)
            self.phase = "HALT"

    def shutdown(self) -> None:
        if self.phase == "BUY_WAIT" and self.order_uuid and self.cfg.cancel_buy_on_exit:
            try:
                self._user_cancel = True
                self.exchange.cancel(self.order_uuid)
                logger.info("종료 전 미체결 매수 %s 를 취소했습니다.", self.order_uuid)
                self._enter_flat()
            except Exception:
                logger.exception("종료 시 매수 취소에 실패했습니다. 거래소에서 직접 확인하세요.")
        elif self.phase in {"SELL_WAIT", "HOLDING"}:
            logger.warning(
                "종료해도 코인 잔고/매도 주문은 그대로 둡니다. uuid=%s. 봇이 꺼지면 손절은 실행되지 않습니다.",
                self.order_uuid,
            )

    def _countdown(self) -> None:
        if self.cfg.dry_run or self.cfg.live_countdown_sec <= 0:
            return
        logger.warning(
            "실전 주문까지 %s초. 중단하려면 Ctrl+C. 마켓 %s, USDT 사용 비율 %s",
            self.cfg.live_countdown_sec,
            self.cfg.market,
            to_plain_str(self.cfg.balance_use_ratio),
        )
        for _ in range(self.cfg.live_countdown_sec):
            if self._stop:
                return
            self._sleep(1)

    def _boot(self) -> None:
        fees = self.exchange.prepare()
        if fees:
            bid, ask = fees
            logger.info("거래소 적용 수수료 | 매수 %s | 매도 %s", to_plain_str(bid), to_plain_str(ask))
            updated = self.strategy.params.__class__(
                **{**self.strategy.params.__dict__, "buy_fee_rate": bid, "sell_fee_rate": ask}
            )
            try:
                updated.validate()
            except ValueError:
                logger.exception("거래소 수수료가 설정 검증에 걸렸습니다. 환경 변수 수수료를 유지합니다.")
            else:
                self.strategy.params = updated
                self.cfg.buy_fee_rate = bid
                self.cfg.sell_fee_rate = ask
        sample = self.strategy.min_sell_price(Decimal("100"))
        logger.info(
            "매수가 100 이면 최소 매도가 %s (왕복 수수료 %.2f%% + 목표 순수익 %.2f%% 이상).",
            to_plain_str(sample),
            float((self.strategy.params.buy_fee_rate + self.strategy.params.sell_fee_rate) * 100),
            float(self.strategy.params.target_profit_rate * 100),
        )
        logger.warning("손절은 이 프로세스가 켜져 있을 때만 동작합니다. 숏 주문은 넣지 않습니다.")
        if self.cfg.balance_use_ratio == 1:
            logger.warning("주문 가능 USDT 를 매수에 사용합니다. 첫 실전은 BALANCE_USE_RATIO 를 낮추세요.")
        if self.cfg.dry_run:
            logger.info(
                "모의거래 모드. 주문 API 는 호출하지 않습니다. 가상 잔고 %s %s",
                to_plain_str(self.cfg.dry_run_quote),
                self.cfg.quote,
            )
            self.phase = "FLAT"
            return
        logger.warning("실전 주문 모드입니다. 마켓 %s", self.cfg.market)
        self._reconcile()

    def _snapshot(self) -> MarketSnapshot:
        now = time.monotonic()
        if not self._candles or now - self._candle_at >= self.cfg.candle_refresh_sec:
            self._candles = self.exchange.get_candles(
                self.cfg.market,
                self.cfg.box_candle_unit,
                self.cfg.box_candle_count,
            )
            self._candle_at = now
        book = self.exchange.get_orderbook(self.cfg.market)
        price = self.feed.get() if self.feed.age() <= self.cfg.price_stale_sec else None
        if price is None:
            price = self.exchange.get_ticker(self.cfg.market).trade_price
        self._last_price = price
        self.exchange.notify_trade_price(price)
        box_low, box_high = box_from_candles(self._candles)
        return MarketSnapshot(
            trade_price=price,
            best_bid=book.best_bid,
            best_ask=book.best_ask,
            bid_size=book.bid_size,
            ask_size=book.ask_size,
            box_low=box_low,
            box_high=box_high,
        )

    def _step_flat(self, snapshot: MarketSnapshot) -> None:
        if time.time() < self.cooldown_until:
            return
        if self._loss_limit_hit():
            logger.error("당일 손실 한도에 도달해 신규 매수를 하지 않습니다.")
            self.phase = "HALT"
            self._save()
            return
        decision = self.strategy.plan_buy(snapshot, self.exchange.quote_available())
        if decision.plan is None:
            self._log_skip(decision.skip_reason or "매수 조건 아님")
            return
        plan = decision.plan
        depth = snapshot.bid_size * plan.price
        if depth < plan.notional:
            logger.warning(
                "최우선 매수 잔량 약 %s %s 가 주문금액 %s 보다 작습니다. 지정가 매수는 대기하지만 시장가 손절은 호가를 잠식합니다.",
                to_plain_str(depth),
                self.cfg.quote,
                to_plain_str(plan.notional),
            )
        spread = (snapshot.best_ask - snapshot.best_bid) / snapshot.best_bid
        if spread > self.strategy.params.target_profit_rate:
            logger.warning(
                "스프레드 %s%% 가 목표 순수익률 %s%% 보다 큽니다.",
                f"{spread * 100:.4f}",
                f"{self.strategy.params.target_profit_rate * 100:.4f}",
            )
        try:
            order = self._submit_buy(plan.price, plan.volume)
        except UpbitAPIError as exc:
            logger.error("매수 주문 실패: %s", exc)
            return
        if order.executed_volume > 0 and not order.is_open:
            self._on_buy_filled(order)
            return
        if not order.is_open:
            logger.warning("매수 주문이 바로 취소되었습니다. 잠시 뒤 다시 시도합니다.")
            self._enter_flat(cooldown=self.cfg.reject_cooldown_sec)
            return
        self.phase = "BUY_WAIT"
        self._track(order)
        logger.info(
            "매수 대기 | %s | 이 가격 체결 시 최소 매도가 %s",
            plan.note,
            to_plain_str(self.strategy.min_sell_price(plan.price)),
        )

    def _step_buy(self, snapshot: MarketSnapshot) -> None:
        order = self._load_order()
        if order is None:
            if self.exchange.base_total() * snapshot.trade_price >= self.cfg.min_order_quote:
                self._adopt_naked_base()
                return
            logger.error("매수 주문을 찾지 못했습니다. 상태를 초기화합니다.")
            self._enter_flat()
            return
        if not order.is_open:
            if order.executed_volume > 0:
                self._on_buy_filled(order)
            else:
                logger.warning("매수 주문이 체결 없이 취소되었습니다.")
                self._enter_flat(cooldown=self.cfg.reject_cooldown_sec)
            return
        if (
            order.executed_volume == 0
            and self.exchange.base_total() * snapshot.trade_price >= self.cfg.min_order_quote
        ):
            logger.warning("매수 주문과 별도로 코인 잔고가 있어 매수를 취소하고 매도로 전환합니다.")
            self._cancel_tracked()
            self._adopt_naked_base()
            return
        if not self._buy_timed_out():
            return
        if order.executed_volume > 0:
            logger.info("부분 체결된 매수를 취소하고 체결분만큼 매도합니다.")
            closed = self._cancel_tracked()
            self._on_buy_filled(closed or order)
            return
        power = self.exchange.quote_available() + self._reserved_quote(order)
        decision = self.strategy.plan_buy(snapshot, power)
        if decision.plan is None:
            logger.info("재호가 조건이 사라져 미체결 매수를 취소합니다. %s", decision.skip_reason)
            self._cancel_tracked()
            self._enter_flat()
            return
        plan = decision.plan
        if plan.price == order.price:
            logger.info("재호가 가격이 동일해 매수 주문을 유지합니다. price=%s", to_plain_str(plan.price))
            self.order_placed_at = time.time()
            self._save()
            return
        replaced = self.exchange.cancel_and_replace_limit(
            order.uuid,
            "bid",
            plan.price,
            plan.volume,
            self.cfg.post_only,
            self.cfg.market,
        )
        if replaced.executed_volume > 0 and not replaced.is_open:
            self._on_buy_filled(replaced)
            return
        if not replaced.is_open:
            self._enter_flat(cooldown=self.cfg.reject_cooldown_sec)
            return
        self.phase = "BUY_WAIT"
        self._track(replaced)
        logger.info("매수 재호가 | price=%s volume=%s", to_plain_str(plan.price), to_plain_str(replaced.volume))

    def _step_holding(self, snapshot: MarketSnapshot) -> None:
        if self.position and self.strategy.should_stop(snapshot.trade_price, self.position.stop_price):
            self._execute_stop(snapshot)
            return
        self._place_take_profit()

    def _step_sell(self, snapshot: MarketSnapshot) -> None:
        order = self._load_order()
        if order is None:
            logger.warning("매도 주문이 보이지 않습니다. 잔고 기준으로 다시 맞춥니다.")
            self._adopt_naked_base()
            return
        if not order.is_open:
            if order.executed_volume > 0:
                self._account_sell(order)
            self._finish_exit(snapshot, "지정가 매도")
            return
        if self.position and self.strategy.should_stop(snapshot.trade_price, self.position.stop_price):
            self._execute_stop(snapshot)
            return
        if time.time() - self.order_placed_at < self.cfg.sell_reprice_sec:
            return
        if order.price is None or self.position is None:
            return
        min_sell = max(self.position.min_sell_price, self.strategy.min_sell_price(self.position.buy_price))
        new_price = self.strategy.next_sell_price(order.price, snapshot.best_ask, min_sell)
        if new_price is None:
            logger.info(
                "최소 매도가 %s 아래로는 내리지 않아 매도 주문 %s 를 유지합니다.",
                to_plain_str(min_sell),
                to_plain_str(order.price),
            )
            self.order_placed_at = time.time()
            self._save()
            return
        replaced = self.exchange.cancel_and_replace_limit(
            order.uuid,
            "ask",
            new_price,
            order.remaining_volume if order.remaining_volume > 0 else order.volume,
            self.cfg.post_only,
            self.cfg.market,
        )
        if not replaced.is_open:
            if replaced.executed_volume > 0:
                self._account_sell(replaced)
            self._finish_exit(snapshot, "지정가 매도")
            return
        self.position.min_sell_price = min_sell
        self._track(replaced)
        logger.info("매도 재호가 | price=%s | 하한 %s", to_plain_str(new_price), to_plain_str(min_sell))

    def _execute_stop(self, snapshot: MarketSnapshot) -> None:
        if self.position is None:
            self._enter_flat()
            return
        logger.warning(
            "손절 | 현재가 %s <= 손절가 %s | 매수가 %s",
            to_plain_str(snapshot.trade_price),
            to_plain_str(self.position.stop_price),
            to_plain_str(self.position.buy_price),
        )
        if self.order_uuid:
            cancelled = self._cancel_tracked()
            if cancelled and cancelled.executed_volume > 0:
                self._account_sell(cancelled)
            self.order_uuid = None
        self._liquidate(snapshot, "손절")

    def _liquidate(self, snapshot: MarketSnapshot, reason: str) -> None:
        self.exchange.begin_step()
        volume = floor_to_precision(self.exchange.base_available(), self.cfg.volume_precision)
        if volume <= 0 or volume * snapshot.trade_price < self.cfg.min_order_quote:
            self._log_pnl(reason)
            return
        try:
            if self.cfg.stop_loss_order_type == "limit":
                price = align_price(snapshot.best_bid, "floor")
                order = self.exchange.sell_limit(self.cfg.market, price, volume, False)
                order = self._wait_closed(order, 3)
                if order.is_open:
                    self._user_cancel = True
                    order = self.exchange.cancel(order.uuid)
                    self._user_cancel = False
                if order.executed_volume > 0 and not order.is_open:
                    self._account_sell(order)
                self.exchange.begin_step()
                volume = floor_to_precision(self.exchange.base_available(), self.cfg.volume_precision)
                if volume > 0 and volume * snapshot.trade_price >= self.cfg.min_order_quote:
                    order = self.exchange.sell_market(self.cfg.market, volume)
                    order = self._wait_closed(order, 5)
                    self._account_sell(order)
            else:
                order = self.exchange.sell_market(self.cfg.market, volume)
                order = self._wait_closed(order, 5)
                self._account_sell(order)
        except UpbitAPIError as exc:
            logger.error("청산 주문 실패: %s", exc)
            if self.position:
                self.position.exit_attempts += 1
                if self.position.exit_attempts >= 3:
                    self.phase = "HALT"
                    self._save()
                    return
            return
        self._finish_exit(snapshot, reason)

    def _finish_exit(self, snapshot: MarketSnapshot, reason: str) -> None:
        self.exchange.begin_step()
        residual = self.exchange.base_total()
        if residual * snapshot.trade_price >= self.cfg.min_order_quote:
            if self.position:
                self.position.exit_attempts += 1
                if self.position.exit_attempts >= 3:
                    logger.error("잔여 수량 청산을 마치지 못했습니다. 중지합니다. residual=%s", to_plain_str(residual))
                    self.phase = "HALT"
                    self._save()
                    return
            logger.warning("잔여 코인 %s 을 다시 청산합니다.", to_plain_str(residual))
            self.order_uuid = None
            self._liquidate(snapshot, reason)
            return
        self._log_pnl(reason)

    def _on_buy_filled(self, order: Order) -> None:
        volume = order.executed_volume
        if volume <= 0:
            self._enter_flat()
            return
        notional = order.funds
        if notional <= 0 and order.price is not None:
            notional = order.price * volume
        fee = order.paid_fee
        if fee <= 0 and notional > 0:
            fee = notional * self.strategy.params.buy_fee_rate
            logger.info("매수 수수료 필드가 비어 설정값으로 추정합니다: %s", to_plain_str(fee))
        buy_price = notional / volume
        minimum = self.strategy.min_sell_price(buy_price)
        self.position = Position(
            cycle_id=uuid.uuid4().hex[:12],
            buy_price=buy_price,
            volume=volume,
            buy_notional=notional,
            buy_fee=fee,
            stop_price=self.strategy.stop_price(buy_price),
            min_sell_price=minimum,
        )
        self.order_uuid = None
        self._seen_sells.clear()
        logger.info(
            "매수 체결 | 가격 %s | 수량 %s | 금액 %s | 수수료 %s | 손절가 %s | 최소매도가 %s",
            to_plain_str(buy_price),
            to_plain_str(volume),
            to_plain_str(notional),
            to_plain_str(fee),
            to_plain_str(self.position.stop_price),
            to_plain_str(minimum),
        )
        self.phase = "HOLDING"
        self._save()
        self._place_take_profit()

    def _place_take_profit(self) -> None:
        if self.position is None:
            self._enter_flat()
            return
        self.exchange.begin_step()
        free = self.exchange.base_available()
        volume = floor_to_precision(min(self.position.volume, free), self.cfg.volume_precision)
        price = max(self.position.min_sell_price, self.strategy.min_sell_price(self.position.buy_price))
        if volume <= 0 or volume * price < self.cfg.min_order_quote:
            if free * self._last_price >= self.cfg.min_order_quote:
                self.phase = "HOLDING"
                self._save()
                logger.info("매도 가능 잔고 반영을 기다립니다.")
                return
            logger.warning("보유 수량이 최소 주문금액보다 작아 매도할 수 없습니다.")
            self.phase = "HALT"
            self._save()
            return
        if price < self.position.min_sell_price:
            price = self.position.min_sell_price
        order = self.exchange.sell_limit(self.cfg.market, price, volume, self.cfg.post_only)
        if not order.is_open and order.executed_volume > 0:
            self._account_sell(order)
            self._log_pnl("지정가 매도")
            return
        if not order.is_open:
            logger.warning("매도 주문이 접수되지 않았습니다.")
            self.phase = "HOLDING"
            self._save()
            return
        self.phase = "SELL_WAIT"
        self._track(order)
        logger.info(
            "매도 대기 | price=%s volume=%s | 매수가 %s 대비 최소 이익 가격",
            to_plain_str(price),
            to_plain_str(volume),
            to_plain_str(self.position.buy_price),
        )

    def _adopt_naked_base(self) -> None:
        self.exchange.begin_step()
        total = self.exchange.base_total()
        price = self._reference_price()
        if total <= 0 or price <= 0 or total * price < self.cfg.min_order_quote:
            self._enter_flat()
            return
        saved = self._read_state()
        saved_pos = (saved or {}).get("position") if saved else None
        if saved_pos and saved_pos.get("buy_price"):
            buy_price = Decimal(saved_pos["buy_price"])
            notional = Decimal(saved_pos["buy_notional"])
            fee = Decimal(saved_pos["buy_fee"])
            estimated = bool(saved_pos.get("estimated", False))
            volume = Decimal(saved_pos.get("volume") or str(total))
        else:
            buy_price = self.exchange.base_avg_buy_price()
            if buy_price <= 0:
                logger.error("매수 단가를 알 수 없어 자동 매도를 넣지 않습니다. 잔고를 직접 확인하세요.")
                self.phase = "HALT"
                self._save()
                return
            volume = total
            notional = buy_price * volume
            fee = notional * self.strategy.params.buy_fee_rate
            estimated = True
            logger.warning("상태 파일이 없어 거래소 평단 %s 으로 매도 기준을 잡습니다. 손익은 추정치입니다.", to_plain_str(buy_price))
        self.position = Position(
            cycle_id=(saved_pos or {}).get("cycle_id") or uuid.uuid4().hex[:12],
            buy_price=buy_price,
            volume=volume,
            buy_notional=notional,
            buy_fee=fee,
            stop_price=self.strategy.stop_price(buy_price),
            min_sell_price=self.strategy.min_sell_price(buy_price),
            estimated=estimated,
        )
        self.order_uuid = None
        self.phase = "HOLDING"
        self._save()
        self._place_take_profit()

    def _reconcile(self) -> None:
        self.exchange.begin_step()
        orders = self.exchange.list_open_orders(self.cfg.market)
        bids = [order for order in orders if order.side == "bid"]
        asks = [order for order in orders if order.side == "ask"]
        if asks:
            for extra in asks[1:]:
                logger.warning("여분 매도 주문을 취소합니다. uuid=%s", extra.uuid)
                self.exchange.cancel(extra.uuid)
            for bid in bids:
                logger.warning("매도 주문이 있어 매수 주문을 취소합니다. uuid=%s", bid.uuid)
                self.exchange.cancel(bid.uuid)
            self._adopt_open_sell(asks[0])
            return
        if bids:
            for extra in bids[1:]:
                logger.warning("여분 매수 주문을 취소합니다. uuid=%s", extra.uuid)
                self.exchange.cancel(extra.uuid)
            self.phase = "BUY_WAIT"
            self._track(bids[0])
            logger.info("미체결 매수 주문을 이어받습니다. uuid=%s", bids[0].uuid)
            return
        self._adopt_naked_base()

    def _adopt_open_sell(self, order: Order) -> None:
        saved = self._read_state()
        saved_pos = (saved or {}).get("position") if saved else None
        if saved_pos and saved_pos.get("buy_price"):
            buy_price = Decimal(saved_pos["buy_price"])
            notional = Decimal(saved_pos["buy_notional"])
            fee = Decimal(saved_pos["buy_fee"])
            estimated = bool(saved_pos.get("estimated", False))
            volume = Decimal(saved_pos.get("volume") or str(order.volume))
        else:
            buy_price = self.exchange.base_avg_buy_price()
            volume = order.volume
            estimated = True
            if buy_price <= 0:
                logger.error(
                    "평단을 알 수 없습니다. 매도 주문 %s 은 유지하고 손절과 가격 인하는 하지 않습니다.",
                    order.uuid,
                )
                buy_price = order.price or Decimal("0")
                notional = buy_price * volume
                fee = Decimal("0")
                minimum = order.price or Decimal("0")
                stop = Decimal("0")
            else:
                notional = buy_price * volume
                fee = notional * self.strategy.params.buy_fee_rate
                minimum = self.strategy.min_sell_price(buy_price)
                stop = self.strategy.stop_price(buy_price)
        if saved_pos and saved_pos.get("buy_price"):
            minimum = self.strategy.min_sell_price(buy_price)
            stop = self.strategy.stop_price(buy_price)
        self.position = Position(
            cycle_id=(saved_pos or {}).get("cycle_id") or uuid.uuid4().hex[:12],
            buy_price=buy_price,
            volume=volume,
            buy_notional=notional,
            buy_fee=fee,
            stop_price=stop,
            min_sell_price=minimum,
            estimated=estimated,
        )
        self.phase = "SELL_WAIT"
        self._track(order)
        logger.info("미체결 매도 주문을 이어받습니다. uuid=%s price=%s", order.uuid, to_plain_str(order.price or Decimal("0")))

    def _submit_buy(self, price: Decimal, volume: Decimal) -> Order:
        current = volume
        last_error: UpbitAPIError | None = None
        for _ in range(3):
            if current * price < self.cfg.min_order_quote:
                break
            try:
                return self.exchange.buy_limit(self.cfg.market, price, current, self.cfg.post_only)
            except UpbitAPIError as exc:
                last_error = exc
                if not exc.insufficient_funds:
                    raise
                shrunk = floor_to_precision(current * Decimal("0.999"), self.cfg.volume_precision)
                if shrunk <= 0 or shrunk >= current:
                    break
                logger.warning("잔고 부족으로 매수 수량을 %s 에서 %s 로 줄입니다.", to_plain_str(current), to_plain_str(shrunk))
                current = shrunk
        if last_error:
            raise last_error
        raise UpbitAPIError("매수 수량을 계산하지 못했습니다.", name="insufficient_funds_bid")

    def _account_sell(self, order: Order) -> None:
        if self.position is None or order.uuid in self._seen_sells:
            return
        if order.is_open or order.executed_volume <= 0:
            return
        self._seen_sells.add(order.uuid)
        self.position.sold_volume += order.executed_volume
        self.position.sold_funds += order.funds
        self.position.sold_fee += order.paid_fee
        logger.info(
            "매도 체결 | uuid=%s price=%s volume=%s 금액=%s 수수료=%s",
            order.uuid,
            to_plain_str(order.price or order.vwap()),
            to_plain_str(order.executed_volume),
            to_plain_str(order.funds),
            to_plain_str(order.paid_fee),
        )

    def _log_pnl(self, reason: str) -> None:
        position = self.position
        if position is None:
            self._enter_flat()
            return
        net, ratio = net_pnl(position.buy_notional, position.buy_fee, position.sold_funds, position.sold_fee)
        self._roll_daily()
        self.realized += net
        self.completed_cycles += 1
        logger.info(
            "청산(%s) | 사이클 %s | 매수가 %s | 수량 %s | 매수금액 %s | 매수수수료 %s | 매도금액 %s | 매도수수료 %s | 순손익 %s %s (%s%%)%s",
            reason,
            position.cycle_id,
            to_plain_str(position.buy_price),
            to_plain_str(position.volume),
            to_plain_str(position.buy_notional),
            to_plain_str(position.buy_fee),
            to_plain_str(position.sold_funds),
            to_plain_str(position.sold_fee),
            to_plain_str(net),
            self.cfg.quote,
            f"{ratio * 100:.4f}",
            " | 복구 추정치" if position.estimated else "",
        )
        if self._loss_limit_hit():
            logger.error(
                "당일 누적 실현손익 %s %s 가 한도 -%s 에 도달했습니다.",
                to_plain_str(self.realized),
                self.cfg.quote,
                to_plain_str(self.cfg.max_daily_loss_quote),
            )
            self.phase = "HALT"
            self.position = None
            self.order_uuid = None
            self._save()
            return
        self._enter_flat(self.cfg.post_stop_cooldown_sec if net < 0 else None)

    def _load_order(self) -> Order | None:
        if not self.order_uuid:
            return None
        return self.exchange.get_order(self.order_uuid)

    def _cancel_tracked(self) -> Order | None:
        if not self.order_uuid:
            return None
        self._user_cancel = True
        try:
            return self.exchange.cancel(self.order_uuid)
        finally:
            self._user_cancel = False

    def _track(self, order: Order) -> None:
        self.order_uuid = order.uuid
        self.order_placed_at = time.time()
        self._save()

    def _buy_timed_out(self) -> bool:
        return time.time() - self.order_placed_at >= self.cfg.buy_timeout_sec

    def _reserved_quote(self, order: Order) -> Decimal:
        if order.side != "bid":
            return Decimal("0")
        if order.locked_funds > 0:
            return order.locked_funds
        if order.price is None:
            return Decimal("0")
        remaining = order.remaining_volume if order.remaining_volume > 0 else order.volume
        return order.price * remaining * (Decimal("1") + self.strategy.params.buy_fee_rate)

    def _wait_closed(self, order: Order, seconds: float) -> Order:
        deadline = time.time() + seconds
        current = order
        while current.is_open and time.time() < deadline and not self._stop:
            time.sleep(0.2)
            if self._last_price > 0:
                self.exchange.notify_trade_price(self._last_price)
            refreshed = self.exchange.get_order(current.uuid)
            if refreshed is None:
                break
            current = refreshed
        return current

    def _reference_price(self) -> Decimal:
        if self._last_price > 0:
            return self._last_price
        return self.exchange.get_ticker(self.cfg.market).trade_price

    def _roll_daily(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._pnl_date:
            self._pnl_date = today
            self.realized = Decimal("0")

    def _loss_limit_hit(self) -> bool:
        limit = self.cfg.max_daily_loss_quote
        return limit > 0 and self.realized <= -limit

    def _log_skip(self, reason: str) -> None:
        now = time.time()
        if reason != self._last_skip or now - self._last_skip_at >= 15:
            logger.info("매수 보류: %s", reason)
            self._last_skip = reason
            self._last_skip_at = now

    def _log_health(self, snapshot: MarketSnapshot) -> None:
        try:
            quote = self.exchange.quote_available()
            base = self.exchange.base_total()
        except Exception:
            logger.exception("상태 로그용 잔고 조회 실패")
            return
        logger.info(
            "상태 %s | 현재가 %s | 박스 %s~%s | %s %s | %s %s | 완료 사이클 %s",
            self.phase,
            to_plain_str(snapshot.trade_price),
            to_plain_str(snapshot.box_low),
            to_plain_str(snapshot.box_high),
            self.cfg.quote,
            to_plain_str(quote),
            self.cfg.base,
            to_plain_str(base),
            self.completed_cycles,
        )

    def _enter_flat(self, cooldown: float | None = None) -> None:
        self.phase = "FLAT"
        self.order_uuid = None
        self.position = None
        if cooldown:
            self.cooldown_until = time.time() + cooldown
        self._save()

    def _save(self) -> None:
        if self.cfg.dry_run:
            return
        position = None
        if self.position is not None:
            position = {
                "cycle_id": self.position.cycle_id,
                "buy_price": to_plain_str(self.position.buy_price),
                "volume": to_plain_str(self.position.volume),
                "buy_notional": to_plain_str(self.position.buy_notional),
                "buy_fee": to_plain_str(self.position.buy_fee),
                "stop_price": to_plain_str(self.position.stop_price),
                "min_sell_price": to_plain_str(self.position.min_sell_price),
                "sold_volume": to_plain_str(self.position.sold_volume),
                "sold_funds": to_plain_str(self.position.sold_funds),
                "sold_fee": to_plain_str(self.position.sold_fee),
                "estimated": self.position.estimated,
            }
        payload = {
            "phase": self.phase,
            "market": self.cfg.market,
            "order_uuid": self.order_uuid,
            "position": position,
        }
        path = Path(self.cfg.state_file)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _read_state(self) -> dict | None:
        path = Path(self.cfg.state_file)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("상태 파일을 읽지 못했습니다.")
            return None
        if data.get("market") != self.cfg.market:
            logger.warning("상태 파일 마켓이 달라 무시합니다: %s", data.get("market"))
            return None
        return data

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(0.2, deadline - time.monotonic()))


def main() -> None:
    cfg = load_config()
    setup_logger(cfg.log_file, cfg.log_level)
    exchange = build_exchange(cfg)
    strategy = Strategy(cfg.to_strategy_params())
    feed: PriceFeed = WebsocketTicker(cfg.market) if cfg.use_websocket else NullFeed()
    bot = RoundTripBot(cfg, exchange, strategy, feed)

    def _handle(signum: int, _frame: object) -> None:
        logger.info("종료 신호 %s 를 받았습니다.", signum)
        bot.request_stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    try:
        bot.run()
    finally:
        feed.close()


if __name__ == "__main__":
    main()
