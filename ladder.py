"""분할 구간(ladder) 매매. 보유 USDT 를 여러 구간으로 나눠 하락 구간별로 사고, 구간별로 올라오면 팝니다.

규칙(backtest_ladder.py 와 동일)
- 앵커(LADDER_ANCHOR_MODE)
  price(기본): 처음 시작할 때의 현재가. 보유 구간이 하나도 없을 때만 시세를 따라 올라갑니다(내려가지는 않음).
  high: 직전 LADDER_LOOKBACK_DAYS 일(완성된 UTC 일봉)의 최고 종가.
- k 번째 구간(1..N) 매수가 = 앵커 * (1 - step)^k. 구간당 투입액 = LADDER_BUDGET_QUOTE / N (고정).
- 체결된 구간은 체결가 * (1 + step) 에 지정가 매도. 매도되면 구간이 비고 다시 매수 대기.
- 손절 없음. 모든 구간이 찬 뒤 더 내리면 평가손실을 그대로 안고 기다립니다.

안전장치
- 봇은 자신이 낸 주문(uuid)으로 산 수량만 팝니다. 계좌의 다른 BTC(원화로 산 장기 보유분 등)는 건드리지 않습니다.
- 투입 총액은 LADDER_BUDGET_QUOTE 를 넘지 않습니다.
- STOP 파일: 새 매수를 멈추고 대기 중인 매수를 취소합니다. 걸어 둔 매도는 유지합니다.
- FLATTEN 파일: 봇의 주문을 모두 취소하고 봇이 산 수량만 시장가로 팝니다. 손실이 확정될 수 있습니다.
- 상태 파일 없이 실계좌에 미체결 주문이 있으면 시작하지 않습니다(중복 주문 방지).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from backfill import DAY_UNIT
from exchange_api import Order, UpbitAPIError
from notifier import Notifier
from strategy import align_price, floor_to_precision, to_plain_str

logger = logging.getLogger("scalper")

EMPTY, BUYING, HOLDING, SELLING = "EMPTY", "BUYING", "HOLDING", "SELLING"
ZERO = Decimal("0")
ONE = Decimal("1")


class LadderConfigError(ValueError):
    pass


@dataclass
class Slot:
    index: int                 # 1..N, 1 이 앵커에 가장 가까운 구간
    status: str = EMPTY
    order_uuid: str = ""
    order_price: str = "0"     # 낸 주문의 가격
    qty: str = "0"             # 보유 수량(체결분)
    entry: str = "0"           # 평균 체결가
    cost: str = "0"            # 매수 대금 + 매수 수수료

    def d(self, name: str) -> Decimal:
        return Decimal(getattr(self, name))


def level_price(anchor: Decimal, step: Decimal, index: int) -> Decimal:
    return align_price(anchor * (ONE - step) ** index, "floor")


def anchor_from_daily(bars: list[Any], lookback: int) -> Decimal:
    """완성된 일봉만 씁니다. 가장 최근 봉은 진행 중인 날이라 제외합니다."""
    ordered = sorted(bars, key=lambda b: b.timestamp_ms)
    done = ordered[:-1][-lookback:]
    if not done:
        raise UpbitAPIError("앵커 계산에 쓸 일봉이 없습니다.", retryable=True)
    return max(Decimal(b.close) for b in done)


def validate_ladder(cfg: Any, buy_fee: Decimal, sell_fee: Decimal) -> Decimal:
    """설정을 확인하고 구간당 투입액을 돌려줍니다."""
    step, slots = cfg.ladder_step_rate, cfg.ladder_slots
    if not 1 <= slots <= 50:
        raise LadderConfigError("LADDER_SLOTS 는 1~50 입니다.")
    if not 2 <= cfg.ladder_lookback_days <= 199:
        raise LadderConfigError("LADDER_LOOKBACK_DAYS 는 2~199 입니다(일봉 한 번 조회 한도).")
    fees = buy_fee + sell_fee
    if step < fees * 2:
        raise LadderConfigError(
            f"LADDER_STEP_RATE({step}) 가 왕복 수수료({fees})의 2배보다 작습니다. 한 번 돌 때 남는 몫이 수수료보다 작아집니다."
        )
    if step >= Decimal("0.5"):
        raise LadderConfigError("LADDER_STEP_RATE 는 0.5 미만이어야 합니다.")
    budget = cfg.ladder_budget_quote if cfg.ladder_budget_quote > 0 else (cfg.dry_run_quote if cfg.dry_run else ZERO)
    if budget <= 0:
        raise LadderConfigError("실전 분할 구간 모드에는 LADDER_BUDGET_QUOTE(투입할 USDT 총액)가 필요합니다.")
    size = budget / slots
    if size < cfg.min_order_quote * 2:
        raise LadderConfigError(f"구간당 투입액 {to_plain_str(size)} 이 최소 주문금액의 2배보다 작습니다. 구간 수를 줄이세요.")
    if cfg.max_order_quote > 0 and size > cfg.max_order_quote:
        raise LadderConfigError(f"구간당 투입액 {to_plain_str(size)} 이 MAX_ORDER_QUOTE 보다 큽니다.")
    return size


class LadderBot:
    def __init__(self, cfg: Any, exchange: Any, notifier: Notifier | None = None):
        self.cfg = cfg
        self.exchange = exchange
        self.notifier = notifier or Notifier()
        self.slots: list[Slot] = [Slot(i) for i in range(1, cfg.ladder_slots + 1)]
        self.anchor = ZERO
        self.anchor_ts = 0.0
        self.realized = ZERO
        self.cycles = 0
        self.buy_fee = cfg.buy_fee_rate
        self.sell_fee = cfg.sell_fee_rate
        self.size = ZERO
        self.flattened = False
        self._stop = False
        self._booted = False
        self._errors = 0
        self._last_heartbeat = float("-inf")
        self._logged: set[str] = set()

    # ---- 실행 ----
    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        self.boot()
        started = time.monotonic()
        while not self._stop:
            if self.cfg.max_runtime_sec and time.monotonic() - started > self.cfg.max_runtime_sec:
                break
            try:
                self.step()
                self._errors = 0
            except UpbitAPIError as exc:
                self._errors += 1
                logger.warning("오류(%d회 연속): %s", self._errors, exc)
                if self._errors == self.cfg.error_alert_threshold:
                    self.notifier.notify(f"오류가 {self._errors}회 연속 발생했습니다: {exc}", key="errors", cooldown=600)
            time.sleep(self.cfg.ladder_poll_sec)
        self.shutdown()

    def boot(self) -> None:
        fees = self.exchange.prepare()
        if fees:
            self.buy_fee, self.sell_fee = fees
        self.size = validate_ladder(self.cfg, self.buy_fee, self.sell_fee)
        restored = self._load()
        if not restored and not self.cfg.dry_run and self.exchange.list_open_orders(self.cfg.market):
            raise LadderConfigError(
                "상태 파일이 없는데 계좌에 미체결 주문이 있습니다. 중복 주문을 막기 위해 시작하지 않습니다. "
                "거래소에서 해당 주문을 정리한 뒤 다시 시작하세요."
            )
        price = self.exchange.get_ticker(self.cfg.market).trade_price
        self._refresh_anchor(price, force=True)
        self._booted = True
        logger.info(
            "분할 구간 시작: 앵커 %s, 간격 %s, 구간 %d개, 구간당 %s %s, 수수료 %s/%s, %s",
            to_plain_str(self.anchor), self.cfg.ladder_step_rate, len(self.slots), to_plain_str(self.size),
            self.cfg.quote, self.buy_fee, self.sell_fee, "모의" if self.cfg.dry_run else "실전",
        )
        self.notifier.notify(
            f"분할 구간 봇 시작({'모의' if self.cfg.dry_run else '실전'}): 앵커 {to_plain_str(self.anchor)}, "
            f"구간 {len(self.slots)}개 x {to_plain_str(self.size)} {self.cfg.quote}"
        )

    def step(self) -> None:
        if not self._booted:
            self.boot()
        self.exchange.begin_step()
        price = self.exchange.get_ticker(self.cfg.market).trade_price
        self.exchange.notify_trade_price(price)
        book = self.exchange.get_orderbook(self.cfg.market)
        if self._flag(self.cfg.flatten_file):
            self._flatten()
            self._heartbeat(price)
            return
        if self.flattened:
            # FLATTEN 파일을 지우면 다시 매수를 시작합니다.
            self.flattened = False
            self._save()
        self._sync_orders()
        self._refresh_anchor(price)
        draining = self._flag(self.cfg.kill_switch_file)
        if draining:
            self._cancel_buys("STOP")
        else:
            self._place_buys(book.best_ask)
        self._place_sells()
        self._heartbeat(price)
        self.notifier.ping()

    def shutdown(self) -> None:
        if self.cfg.cancel_buy_on_exit:
            self._cancel_buys("종료")
        self._save()
        held = [s for s in self.slots if s.status in (HOLDING, SELLING)]
        logger.info("종료: 보유 구간 %d개, 걸어 둔 매도 주문은 유지합니다. 실현손익 %s", len(held), to_plain_str(self.realized))

    # ---- 앵커 ----
    def _refresh_anchor(self, price: Decimal, force: bool = False) -> None:
        if self.cfg.ladder_anchor_mode == "price":
            anchor = self.anchor
            if anchor <= 0:
                anchor = price  # 처음 시작: 현재가가 기준
            elif self._flat() and price >= anchor * (ONE + self.cfg.ladder_reanchor_rate):
                anchor = price  # 보유 구간이 없을 때만 시세를 따라 올림
        else:
            now = time.time()
            if not force and now - self.anchor_ts < self.cfg.ladder_anchor_refresh_sec:
                return
            bars = self.exchange.get_ohlcv_page(self.cfg.market, DAY_UNIT, self.cfg.ladder_lookback_days + 1)
            anchor = anchor_from_daily(bars, self.cfg.ladder_lookback_days)
            self.anchor_ts = now
        if anchor != self.anchor:
            logger.info("앵커 변경: %s -> %s", to_plain_str(self.anchor), to_plain_str(anchor))
            self.anchor = anchor
            self._save()

    def _flat(self) -> bool:
        return all(s.status in (EMPTY, BUYING) and s.d("qty") <= 0 for s in self.slots)

    # ---- 주문 상태 반영 ----
    def _sync_orders(self) -> None:
        for slot in self.slots:
            if slot.status not in (BUYING, SELLING) or not slot.order_uuid:
                continue
            order = self.exchange.get_order(slot.order_uuid)
            if order is None or order.is_open:
                continue
            if slot.status == BUYING:
                self._on_buy_closed(slot, order)
            else:
                self._on_sell_closed(slot, order)
            self._save()

    def _on_buy_closed(self, slot: Slot, order: Order) -> None:
        if order.executed_volume <= 0:
            self._clear(slot)
            return
        funds = order.funds
        fee = order.paid_fee if order.paid_fee > 0 else funds * self.buy_fee
        slot.status, slot.order_uuid, slot.order_price = HOLDING, "", "0"
        slot.qty = to_plain_str(slot.d("qty") + order.executed_volume)
        slot.cost = to_plain_str(slot.d("cost") + funds + fee)
        slot.entry = to_plain_str(order.vwap())
        logger.info("구간 %d 매수 체결: %s @ %s", slot.index, to_plain_str(order.executed_volume), slot.entry)
        self.notifier.notify(f"구간 {slot.index} 매수 체결 {to_plain_str(order.executed_volume)} {self.cfg.base} @ {slot.entry}")

    def _on_sell_closed(self, slot: Slot, order: Order) -> None:
        sold = order.executed_volume
        qty = slot.d("qty")
        if sold <= 0:
            slot.status, slot.order_uuid, slot.order_price = HOLDING, "", "0"
            return
        funds = order.funds
        fee = order.paid_fee if order.paid_fee > 0 else funds * self.sell_fee
        share = slot.d("cost") * (sold / qty) if qty > 0 else slot.d("cost")
        pnl = funds - fee - share
        self.realized += pnl
        remain = qty - sold
        if remain > 0:
            # 밖에서 취소된 부분 체결. 남은 수량은 다시 매도 주문을 냅니다.
            slot.status, slot.order_uuid, slot.order_price = HOLDING, "", "0"
            slot.qty, slot.cost = to_plain_str(remain), to_plain_str(slot.d("cost") - share)
        else:
            self.cycles += 1
            self._clear(slot)
        logger.info("구간 %d 매도 체결: %s, 손익 %s (누적 %s)", slot.index, to_plain_str(sold), to_plain_str(pnl), to_plain_str(self.realized))
        self.notifier.notify(
            f"구간 {slot.index} 매도 체결, 손익 {to_plain_str(pnl.quantize(Decimal('0.0001')))} {self.cfg.quote} "
            f"(누적 {to_plain_str(self.realized.quantize(Decimal('0.0001')))})"
        )

    # ---- 주문 내기 ----
    def _place_buys(self, best_ask: Decimal) -> None:
        open_buys = 0
        for slot in self.slots:
            if open_buys >= self.cfg.ladder_max_open_buys:
                if slot.status == BUYING:
                    self._cancel_buy(slot, "대기 한도")
                continue
            if slot.status not in (EMPTY, BUYING):
                continue
            # 시세가 이미 구간 가격 아래면 매도 1호가에 냅니다(구간 가격보다 비싸게 사지 않음).
            price = min(level_price(self.anchor, self.cfg.ladder_step_rate, slot.index), align_price(best_ask, "floor"))
            if slot.status == BUYING:
                open_buys += 1
                if slot.d("order_price") == price:
                    continue
                order = self.exchange.get_order(slot.order_uuid)
                if order is not None and (order.executed_volume > 0 or not order.is_open):
                    continue  # 부분 체결 중이거나 이미 닫힌 주문은 다음 틱의 _sync_orders 가 처리
                self._cancel_buy(slot, "가격 재설정")
                if slot.status != EMPTY:
                    continue
                open_buys -= 1
            volume = floor_to_precision(self.size / (price * (ONE + self.buy_fee)), self.cfg.volume_precision)
            need = price * volume * (ONE + self.buy_fee)
            if volume <= 0 or price * volume < self.cfg.min_order_quote:
                continue
            if self.exchange.quote_available(self.cfg.quote) < need:
                self._once("funds", "주문 가능 %s 가 부족해 구간 %d 매수를 내지 못했습니다.", self.cfg.quote, slot.index)
                break
            # 매도 1호가에 내는 주문은 즉시 체결돼야 하므로 post_only 를 쓰지 않습니다.
            order = self.exchange.buy_limit(self.cfg.market, price, volume, self.cfg.post_only and price < best_ask)
            slot.status, slot.order_uuid, slot.order_price = BUYING, order.uuid, to_plain_str(price)
            open_buys += 1
            self._logged.discard("funds")
            self._save()

    def _place_sells(self) -> None:
        for slot in self.slots:
            if slot.status != HOLDING:
                continue
            qty = slot.d("qty")
            price = align_price(slot.d("entry") * (ONE + self.cfg.ladder_step_rate), "ceil")
            if qty <= 0 or price * qty < self.cfg.min_order_quote:
                self._once(f"dust{slot.index}", "구간 %d 잔량 %s 이 최소 주문금액보다 작아 매도 주문을 내지 못합니다.", slot.index, slot.qty)
                continue
            try:
                order = self.exchange.sell_limit(self.cfg.market, price, qty, self.cfg.post_only)
            except UpbitAPIError as exc:
                if exc.name == "insufficient_funds_ask":
                    self._once(
                        f"ask{slot.index}",
                        "구간 %d 매도 실패: 계좌의 %s 주문 가능 수량이 봇 기록(%s)보다 적습니다. 수동 매도 여부를 확인하세요.",
                        slot.index, self.cfg.base, slot.qty,
                    )
                    self.notifier.notify(f"구간 {slot.index} 매도 주문 실패(잔고 부족). 계좌를 확인하세요.", key=f"ask{slot.index}", cooldown=3600)
                    continue
                raise
            slot.status, slot.order_uuid, slot.order_price = SELLING, order.uuid, to_plain_str(price)
            self._save()

    def _cancel_buy(self, slot: Slot, reason: str) -> None:
        order = self.exchange.cancel(slot.order_uuid)
        logger.info("구간 %d 매수 취소(%s)", slot.index, reason)
        self._on_buy_closed(slot, order)
        self._save()

    def _cancel_buys(self, reason: str) -> None:
        for slot in self.slots:
            if slot.status == BUYING:
                self._cancel_buy(slot, reason)

    def _flatten(self) -> None:
        if self.flattened:
            return
        self._cancel_buys("FLATTEN")
        total = ZERO
        for slot in self.slots:
            if slot.status == SELLING:
                order = self.exchange.cancel(slot.order_uuid)
                self._on_sell_closed(slot, order)
            if slot.status == HOLDING:
                total += slot.d("qty")
        if total > 0:
            volume = floor_to_precision(total, self.cfg.volume_precision)
            order = self.exchange.sell_market(self.cfg.market, volume)
            funds = order.funds
            fee = order.paid_fee if order.paid_fee > 0 else funds * self.sell_fee
            cost = sum((s.d("cost") for s in self.slots if s.status == HOLDING), ZERO)
            self.realized += funds - fee - cost
            for slot in self.slots:
                if slot.status == HOLDING:
                    self._clear(slot)
        self.flattened = True
        self._save()
        logger.warning("FLATTEN: 봇 보유분 %s 을 시장가로 정리했습니다. 누적 실현손익 %s", to_plain_str(total), to_plain_str(self.realized))
        self.notifier.notify(f"FLATTEN 완료. 누적 실현손익 {to_plain_str(self.realized.quantize(Decimal('0.0001')))} {self.cfg.quote}. 새 매수는 멈춘 상태입니다.")

    # ---- 보조 ----
    @staticmethod
    def _clear(slot: Slot) -> None:
        slot.status, slot.order_uuid, slot.order_price = EMPTY, "", "0"
        slot.qty, slot.entry, slot.cost = "0", "0", "0"

    def held_volume(self) -> Decimal:
        return sum((s.d("qty") for s in self.slots if s.status in (HOLDING, SELLING)), ZERO)

    def _flag(self, name: str) -> bool:
        return bool(name) and Path(name).exists()

    def _once(self, key: str, message: str, *args: object) -> None:
        if key not in self._logged:
            self._logged.add(key)
            logger.warning(message, *args)

    def _heartbeat(self, price: Decimal) -> None:
        path = self.cfg.heartbeat_file
        now = time.monotonic()
        if not path or now - self._last_heartbeat < 10:
            return
        self._last_heartbeat = now
        counts = {name: sum(1 for s in self.slots if s.status == name) for name in (EMPTY, BUYING, HOLDING, SELLING)}
        try:
            Path(path).write_text(
                json.dumps({"ts": time.time(), "phase": "LADDER", "market": self.cfg.market, "price": to_plain_str(price),
                            "anchor": to_plain_str(self.anchor), "slots": counts, "realized": to_plain_str(self.realized)}),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("하트비트 파일을 쓰지 못했습니다: %s", path)

    def _save(self) -> None:
        data = {
            "version": 1,
            "market": self.cfg.market,
            "dry_run": bool(self.cfg.dry_run),
            "step": to_plain_str(self.cfg.ladder_step_rate),
            "anchor": to_plain_str(self.anchor),
            "anchor_mode": self.cfg.ladder_anchor_mode,
            "realized": to_plain_str(self.realized),
            "cycles": self.cycles,
            "flattened": self.flattened,
            "slots": [asdict(s) for s in self.slots],
        }
        path = Path(self.cfg.ladder_state_file)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def _load(self) -> bool:
        path = Path(self.cfg.ladder_state_file)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LadderConfigError(f"상태 파일 {path} 을 읽지 못했습니다: {exc}") from exc
        if data.get("market") != self.cfg.market or bool(data.get("dry_run")) != bool(self.cfg.dry_run):
            raise LadderConfigError(f"상태 파일 {path} 이 현재 설정(마켓 또는 모의/실전)과 다릅니다. 파일을 옮긴 뒤 시작하세요.")
        if self.cfg.dry_run:
            return False  # 모의 주문은 메모리에만 있어 이어받을 수 없습니다. 새로 시작합니다.
        saved = [Slot(**row) for row in data.get("slots", [])]
        active = [s for s in saved if s.status != EMPTY]
        if any(s.index > len(self.slots) for s in active):
            raise LadderConfigError("상태 파일에 현재 LADDER_SLOTS 보다 큰 구간의 포지션이 있습니다. 구간 수를 줄이기 전에 정리하세요.")
        if active and Decimal(str(data.get("step", "0"))) != self.cfg.ladder_step_rate:
            logger.warning("LADDER_STEP_RATE 가 바뀌었습니다. 이미 걸린 매도 주문은 예전 간격 그대로 둡니다.")
        for s in active:
            self.slots[s.index - 1] = s
        if data.get("anchor_mode", "high") == self.cfg.ladder_anchor_mode == "price":
            self.anchor = Decimal(str(data.get("anchor", "0")))  # 재시작해도 구간 가격이 움직이지 않게 이어받음
        self.realized = Decimal(str(data.get("realized", "0")))
        self.cycles = int(data.get("cycles", 0))
        self.flattened = bool(data.get("flattened", False))
        logger.info("상태 복원: 진행 중 구간 %d개, 누적 실현손익 %s", len(active), to_plain_str(self.realized))
        return True
