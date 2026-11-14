"""업비트 시세·주문 REST 와 현재가 WebSocket.

pyupbit 의 get_tick_size 는 원화 마켓 호가표라 USDT-BTC(호가 0.01)에 쓰면
가격이 틀어집니다. 그래서 공식 REST/WebSocket 을 직접 호출합니다.
시세는 초당 10회, 주문 생성은 초당 12회, 잔고·조회는 초당 30회가 상한입니다.
이 클라이언트는 그보다 낮게 간격을 두고, Remaining-Req 가 바닥이면 쉽니다.
Origin 헤더는 시세 한도를 더 세게 조이므로 보내지 않습니다.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping
from urllib.parse import unquote, urlencode

import jwt
import requests

from backfill import DAY_UNIT, OhlcvBar, parse_upbit_candle
from logger import get_logger
from strategy import Candle, align_price, to_plain_str

logger = get_logger()

BASE_URL = "https://api.upbit.com"
PUBLIC_WS = "wss://api.upbit.com/websocket/v1"


class UpbitAPIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        name: str = "",
        payload: Any = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.name = name
        self.payload = payload
        self.retryable = retryable
        self.retry_after = retry_after
        self.insufficient_funds = name.startswith("insufficient_funds")


@dataclass
class Ticker:
    market: str
    trade_price: Decimal


@dataclass
class OrderBook:
    market: str
    best_bid: Decimal
    best_ask: Decimal
    bid_size: Decimal
    ask_size: Decimal


@dataclass
class Order:
    uuid: str
    identifier: str
    market: str
    side: str
    ord_type: str
    state: str
    price: Decimal | None
    volume: Decimal
    executed_volume: Decimal
    remaining_volume: Decimal
    paid_fee: Decimal
    locked_funds: Decimal
    trades: list[dict[str, Any]] = field(default_factory=list)
    created_ts: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state in {"wait", "watch"}

    @property
    def funds(self) -> Decimal:
        total = Decimal("0")
        for trade in self.trades:
            total += _dec(trade.get("funds", "0"))
        if total > 0:
            return total
        if self.price is not None and self.executed_volume > 0:
            return self.price * self.executed_volume
        return Decimal("0")

    def vwap(self) -> Decimal:
        if self.executed_volume <= 0:
            raise ValueError("체결 수량이 없습니다.")
        return self.funds / self.executed_volume


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _identifier(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"[:64]


def _encode_params(params: Mapping[str, str]) -> str:
    return unquote(urlencode(list(params.items()), doseq=True))


def _error_parts(body: Any) -> tuple[str, str]:
    if isinstance(body, dict):
        nested = body.get("error")
        source = nested if isinstance(nested, dict) else body
        name = str(source.get("name") or "")
        message = str(source.get("message") or body)
        return name, message
    return "", str(body)[:300]


def order_from_payload(data: dict[str, Any]) -> Order:
    price = data.get("price")
    trades = list(data.get("trades") or [])
    return Order(
        uuid=str(data.get("uuid") or ""),
        identifier=str(data.get("identifier") or ""),
        market=str(data.get("market") or ""),
        side=str(data.get("side") or ""),
        ord_type=str(data.get("ord_type") or ""),
        state=str(data.get("state") or ""),
        price=_dec(price) if price not in (None, "") else None,
        volume=_dec(data.get("volume")),
        executed_volume=_dec(data.get("executed_volume")),
        remaining_volume=_dec(data.get("remaining_volume")),
        paid_fee=_dec(data.get("paid_fee")),
        locked_funds=_dec(data.get("locked")),
        trades=trades,
        created_ts=time.time(),
        raw=dict(data),
    )


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next = max(self._next, now) + self.interval


class PriceFeed:
    def get(self) -> Decimal | None:
        raise NotImplementedError

    def age(self) -> float:
        raise NotImplementedError

    def close(self) -> None:
        return None


class NullFeed(PriceFeed):
    def get(self) -> Decimal | None:
        return None

    def age(self) -> float:
        return 1e9


class WebsocketTicker(PriceFeed):
    """공개 현재가 스트림. 끊기면 재연결하고, 실패해도 REST 현재가로 버틸 수 있게 둡니다."""

    def __init__(self, market: str):
        self.market = market
        self._lock = threading.Lock()
        self._price: Decimal | None = None
        self._updated = 0.0
        self._backoff = 1.0
        self._stop = threading.Event()
        self._ws: Any = None
        self._thread = threading.Thread(target=self._run, name="upbit-ticker", daemon=True)
        self._thread.start()

    def get(self) -> Decimal | None:
        with self._lock:
            return self._price

    def age(self) -> float:
        with self._lock:
            if self._updated <= 0:
                return 1e9
            return time.monotonic() - self._updated

    def close(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                logger.exception("웹소켓 종료 중 오류")
        self._thread.join(timeout=5)

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            logger.error("websocket-client 가 없습니다. REST 현재가만 사용합니다.")
            return

        while not self._stop.is_set():
            try:
                app = websocket.WebSocketApp(
                    PUBLIC_WS,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws = app
                app.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                logger.exception("웹소켓 루프 오류")
            if self._stop.is_set():
                break
            logger.warning("현재가 웹소켓이 끊겼습니다. %.1f초 후 재연결합니다.", self._backoff)
            self._stop.wait(self._backoff)
            self._backoff = min(self._backoff * 2, 30)

    def _on_open(self, ws: Any) -> None:
        request = [
            {"ticket": "roundtrip-scalper"},
            {"type": "ticker", "codes": [self.market]},
            {"format": "DEFAULT"},
        ]
        ws.send(json.dumps(request))
        self._backoff = 1.0
        logger.info("현재가 웹소켓 연결: %s", self.market)

    def _on_message(self, _ws: Any, message: Any) -> None:
        try:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            data = json.loads(message, parse_float=Decimal)
            if data.get("type") not in {None, "ticker"} and "trade_price" not in data:
                return
            price = data.get("trade_price")
            if price is None:
                return
            with self._lock:
                self._price = _dec(price)
                self._updated = time.monotonic()
        except Exception:
            logger.exception("웹소켓 메시지 파싱 실패")

    def _on_error(self, _ws: Any, error: Any) -> None:
        if not self._stop.is_set():
            logger.warning("웹소켓 오류: %s", error)

    def _on_close(self, _ws: Any, status: Any, msg: Any) -> None:
        if not self._stop.is_set():
            logger.info("웹소켓 종료 status=%s msg=%s", status, msg)


class UpbitREST:
    def __init__(self, access_key: str, secret_key: str, http_timeout: float, max_retries: int, retry_delay: float, quotation_rps: float, account_rps: float, order_rps: float):
        self.access_key = access_key
        self.secret_key = secret_key
        self.http_timeout = http_timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.session = requests.Session()
        self.limiters = {
            "quotation": RateLimiter(quotation_rps),
            "default": RateLimiter(account_rps),
            "order": RateLimiter(order_rps),
        }
        self._balance_cache: list[dict[str, Any]] | None = None

    def begin_step(self) -> None:
        self._balance_cache = None

    def prepare(self) -> tuple[Decimal, Decimal] | None:
        return None

    def _headers(self, params: Mapping[str, str] | None) -> dict[str, str]:
        payload: dict[str, str] = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if params:
            query = _encode_params(params)
            payload["query_hash"] = hashlib.sha512(query.encode("utf-8")).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _honor_remaining(self, headers: Mapping[str, str]) -> None:
        raw = headers.get("Remaining-Req") or headers.get("remaining-req") or ""
        marker = "sec="
        if marker not in raw:
            return
        try:
            sec = int(raw.split(marker, 1)[1].split(";", 1)[0].strip())
        except ValueError:
            return
        if sec <= 0:
            logger.warning("Remaining-Req 잔여 0 (%s). 1초 대기합니다.", raw)
            time.sleep(1.05)

    def _request(self, method: str, path: str, params: dict[str, str] | None, group: str, auth: bool) -> Any:
        self.limiters[group].acquire()
        headers = self._headers(params if auth else None) if auth else {"Accept": "application/json"}
        url = BASE_URL + path
        try:
            if method in {"GET", "DELETE"}:
                if params:
                    url = f"{url}?{_encode_params(params)}"
                response = self.session.request(method, url, headers=headers, timeout=self.http_timeout)
            elif method == "POST":
                response = self.session.post(url, headers=headers, json=params or {}, timeout=self.http_timeout)
            else:
                raise ValueError(method)
        except requests.RequestException:
            raise

        self._honor_remaining(response.headers)
        text = response.text or ""
        try:
            body = json.loads(text, parse_float=Decimal) if text else None
        except json.JSONDecodeError:
            body = {"raw": text[:500]}

        if response.status_code in {429, 418} or response.status_code >= 500 or response.status_code == 408:
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else None
            name, message = _error_parts(body)
            raise UpbitAPIError(
                f"HTTP {response.status_code} {name} {message}".strip(),
                status=response.status_code,
                name=name,
                payload=body,
                retryable=True,
                retry_after=delay,
            )
        if not 200 <= response.status_code < 300:
            name, message = _error_parts(body)
            raise UpbitAPIError(
                f"HTTP {response.status_code} {name} {message}".strip(),
                status=response.status_code,
                name=name,
                payload=body,
                retryable=False,
            )
        return body

    def _retry(self, label: str, func: Any) -> Any:
        delay = self.retry_delay
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return func()
            except (requests.RequestException, UpbitAPIError) as exc:
                last = exc
                retryable = isinstance(exc, requests.RequestException) or exc.retryable
                if not retryable or attempt >= self.max_retries:
                    raise
                pause = exc.retry_after if isinstance(exc, UpbitAPIError) and exc.retry_after else delay
                if isinstance(exc, UpbitAPIError) and exc.status == 418:
                    pause = max(pause or 0, 30)
                logger.warning("%s 실패 (%s/%s): %s — %.1f초 후 재시도", label, attempt, self.max_retries, exc, pause)
                time.sleep(pause)
                delay = min(delay * 2, 30)
        if last:
            raise last
        raise UpbitAPIError(f"{label} 재시도 실패", retryable=False)

    def get_ticker(self, market: str) -> Ticker:
        def call() -> Ticker:
            body = self._request("GET", "/v1/ticker", {"markets": market}, "quotation", auth=False)
            row = body[0]
            return Ticker(market=str(row["market"]), trade_price=_dec(row["trade_price"]))

        return self._retry("현재가 조회", call)

    def get_orderbook(self, market: str) -> OrderBook:
        def call() -> OrderBook:
            body = self._request("GET", "/v1/orderbook", {"markets": market}, "quotation", auth=False)
            units = body[0].get("orderbook_units") or []
            if not units:
                raise UpbitAPIError("호가가 비어 있습니다.", retryable=True)
            best = units[0]
            return OrderBook(
                market=market,
                best_bid=_dec(best["bid_price"]),
                best_ask=_dec(best["ask_price"]),
                bid_size=_dec(best["bid_size"]),
                ask_size=_dec(best["ask_size"]),
            )

        return self._retry("호가 조회", call)

    def get_ohlcv_page(
        self,
        market: str,
        unit: int,
        count: int = 200,
        to: str | None = None,
    ) -> list[OhlcvBar]:
        """분봉/일봉 한 페이지. to 는 그 시각보다 과거 캔들만 받는 업비트 커서입니다.

        unit 이 DAY_UNIT(1440) 이면 /v1/candles/days, 그 외는 분봉 API 를 씁니다.
        시각은 candle_date_time_utc(UTC) 기준입니다.
        """
        safe_count = max(1, min(int(count), 200))
        if int(unit) == DAY_UNIT:
            path = "/v1/candles/days"
        else:
            path = f"/v1/candles/minutes/{unit}"

        def call() -> list[OhlcvBar]:
            params = {"market": market, "count": str(safe_count)}
            if to:
                params["to"] = to
            body = self._request(
                "GET",
                path,
                params,
                "quotation",
                auth=False,
            )
            if not isinstance(body, list):
                raise UpbitAPIError("캔들 응답 형식이 다릅니다.", retryable=True)
            if not body:
                # 페이지 커서가 상장 이전으로 내려가면 빈 목록이 정상입니다.
                # 최신 봉 요청이 비면 일시 오류일 수 있어 재시도합니다.
                if to:
                    return []
                raise UpbitAPIError("캔들이 비어 있습니다.", retryable=True)
            return [parse_upbit_candle(row) for row in body if isinstance(row, dict)]

        return self._retry("캔들 조회", call)

    def get_candles(self, market: str, unit: int, count: int) -> list[Candle]:
        bars = self.get_ohlcv_page(market, unit, count)
        return [Candle(open=bar.open, high=bar.high, low=bar.low, close=bar.close) for bar in bars]

    def _accounts(self) -> list[dict[str, Any]]:
        if self._balance_cache is None:
            body = self._retry("잔고 조회", lambda: self._request("GET", "/v1/accounts", None, "default", auth=True))
            if not isinstance(body, list):
                raise UpbitAPIError(f"잔고 응답 형식이 다릅니다: {body}", retryable=True)
            self._balance_cache = body
        return self._balance_cache

    def _account(self, currency: str, unit_currency: str | None) -> dict[str, Any] | None:
        for row in self._accounts():
            if str(row.get("currency")) != currency:
                continue
            if unit_currency is not None and str(row.get("unit_currency")) != unit_currency:
                continue
            return row
        return None

    def quote_available(self, quote: str) -> Decimal:
        row = self._account(quote, None)
        return _dec(row.get("balance")) if row else Decimal("0")

    def quote_locked(self, quote: str) -> Decimal:
        row = self._account(quote, None)
        return _dec(row.get("locked")) if row else Decimal("0")

    def base_available(self, base: str, quote: str) -> Decimal:
        row = self._account(base, quote)
        return _dec(row.get("balance")) if row else Decimal("0")

    def base_total(self, base: str, quote: str) -> Decimal:
        row = self._account(base, quote)
        if not row:
            return Decimal("0")
        return _dec(row.get("balance")) + _dec(row.get("locked"))

    def base_avg_buy_price(self, base: str, quote: str) -> Decimal:
        row = self._account(base, quote)
        return _dec(row.get("avg_buy_price")) if row else Decimal("0")

    def fetch_fees(self, market: str) -> tuple[Decimal, Decimal] | None:
        try:
            body = self._retry(
                "수수료 조회",
                lambda: self._request("GET", "/v1/orders/chance", {"market": market}, "default", auth=True),
            )
        except (requests.RequestException, UpbitAPIError) as exc:
            logger.warning("주문 가능 정보 조회 실패. 설정의 수수료를 사용합니다: %s", exc)
            return None
        if not isinstance(body, dict):
            return None
        bid = body.get("bid_fee")
        ask = body.get("ask_fee")
        if bid is None or ask is None:
            return None
        return _dec(bid), _dec(ask)

    def _find_by_identifier(self, identifier: str) -> Order | None:
        try:
            body = self._request("GET", "/v1/order", {"identifier": identifier}, "default", auth=True)
        except UpbitAPIError as exc:
            if exc.status in {400, 404}:
                return None
            logger.warning("identifier 조회 실패: %s", exc)
            return None
        except requests.RequestException as exc:
            logger.warning("identifier 조회 네트워크 오류: %s", exc)
            return None
        if not isinstance(body, dict) or not body.get("uuid"):
            return None
        return order_from_payload(body)

    def _submit(self, params: dict[str, str]) -> Order:
        identifier = params["identifier"]
        delay = self.retry_delay
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                body = self._request("POST", "/v1/orders", params, "order", auth=True)
                order = order_from_payload(body)
                self._log_order("주문 응답", order)
                return order
            except requests.RequestException as exc:
                last = exc
                recovered = self._find_by_identifier(identifier)
                if recovered:
                    logger.warning("주문 요청이 불확실해 identifier 로 복구했습니다: %s", identifier)
                    return recovered
            except UpbitAPIError as exc:
                last = exc
                recovered = self._find_by_identifier(identifier)
                if recovered:
                    logger.warning("주문 오류 후 기존 identifier 주문을 사용합니다: %s", identifier)
                    return recovered
                if exc.insufficient_funds or not exc.retryable or attempt >= self.max_retries:
                    raise
                if exc.retry_after:
                    delay = max(delay, exc.retry_after)
                if exc.status == 418:
                    delay = max(delay, 30)
            logger.warning("주문 전송 재시도 (%s/%s): %s", attempt, self.max_retries, last)
            time.sleep(delay)
            delay = min(delay * 2, 30)
        if isinstance(last, Exception):
            raise last
        raise UpbitAPIError("주문 전송 실패", retryable=False)

    def _log_order(self, label: str, order: Order) -> None:
        logger.info(
            "%s | market=%s side=%s type=%s uuid=%s state=%s price=%s volume=%s executed=%s fee=%s",
            label,
            order.market,
            order.side,
            order.ord_type,
            order.uuid,
            order.state,
            to_plain_str(order.price) if order.price is not None else "-",
            to_plain_str(order.volume),
            to_plain_str(order.executed_volume),
            to_plain_str(order.paid_fee),
        )

    def buy_limit(self, market: str, price: Decimal, volume: Decimal, post_only: bool) -> Order:
        params = {
            "market": market,
            "side": "bid",
            "ord_type": "limit",
            "price": to_plain_str(price),
            "volume": to_plain_str(volume),
            "identifier": _identifier("b"),
        }
        if post_only:
            params["time_in_force"] = "post_only"
        logger.info("주문 요청 | 지정가 매수 %s price=%s volume=%s id=%s", market, params["price"], params["volume"], params["identifier"])
        self._balance_cache = None
        return self._submit(params)

    def sell_limit(self, market: str, price: Decimal, volume: Decimal, post_only: bool) -> Order:
        params = {
            "market": market,
            "side": "ask",
            "ord_type": "limit",
            "price": to_plain_str(price),
            "volume": to_plain_str(volume),
            "identifier": _identifier("s"),
        }
        if post_only:
            params["time_in_force"] = "post_only"
        logger.info("주문 요청 | 지정가 매도 %s price=%s volume=%s id=%s", market, params["price"], params["volume"], params["identifier"])
        self._balance_cache = None
        return self._submit(params)

    def sell_market(self, market: str, volume: Decimal) -> Order:
        params = {
            "market": market,
            "side": "ask",
            "ord_type": "market",
            "volume": to_plain_str(volume),
            "identifier": _identifier("m"),
        }
        logger.info("주문 요청 | 시장가 매도 %s volume=%s id=%s", market, params["volume"], params["identifier"])
        self._balance_cache = None
        return self._submit(params)

    def get_order(self, order_uuid: str) -> Order | None:
        def call() -> Any:
            return self._request("GET", "/v1/order", {"uuid": order_uuid}, "default", auth=True)

        try:
            body = self._retry("주문 조회", call)
        except UpbitAPIError as exc:
            if exc.status in {400, 404}:
                return None
            raise
        if not isinstance(body, dict) or not body.get("uuid"):
            return None
        return order_from_payload(body)

    def list_open_orders(self, market: str) -> list[Order]:
        def call() -> Any:
            return self._request(
                "GET",
                "/v1/orders/open",
                {"market": market, "limit": "100"},
                "default",
                auth=True,
            )

        try:
            body = self._retry("미체결 조회", call)
        except UpbitAPIError as exc:
            if exc.status != 404:
                raise
            body = self._retry(
                "미체결 조회",
                lambda: self._request(
                    "GET",
                    "/v1/orders",
                    {"market": market, "state": "wait", "limit": "100"},
                    "default",
                    auth=True,
                ),
            )
        if not isinstance(body, list):
            raise UpbitAPIError(f"미체결 응답 형식이 다릅니다: {body}", retryable=True)
        return [order_from_payload(row) for row in body if isinstance(row, dict)]

    def cancel(self, order_uuid: str) -> Order:
        logger.info("주문 취소 요청 | uuid=%s", order_uuid)

        def call() -> Any:
            return self._request("DELETE", "/v1/order", {"uuid": order_uuid}, "default", auth=True)

        try:
            body = self._retry("주문 취소", call)
        except UpbitAPIError as exc:
            current = self.get_order(order_uuid)
            if current and not current.is_open:
                logger.info("주문은 이미 종료되어 있습니다 | uuid=%s state=%s", order_uuid, current.state)
                return current
            raise exc
        self._balance_cache = None
        order = order_from_payload(body)
        self._log_order("주문 취소 응답", order)
        return order

    def cancel_and_replace_limit(
        self,
        order_uuid: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        post_only: bool,
        market: str,
    ) -> Order:
        params = {
            "prev_order_uuid": order_uuid,
            "new_ord_type": "limit",
            "new_price": to_plain_str(price),
            "new_volume": to_plain_str(volume),
            "new_identifier": _identifier("r"),
        }
        if post_only:
            params["new_time_in_force"] = "post_only"
        logger.info(
            "취소 후 재주문 요청 | prev=%s side=%s price=%s volume=%s",
            order_uuid,
            side,
            params["new_price"],
            params["new_volume"],
        )
        try:
            body = self._request("POST", "/v1/orders/cancel_and_new", params, "order", auth=True)
        except UpbitAPIError as exc:
            if exc.status != 404:
                raise
            logger.warning("cancel_and_new 을 사용할 수 없어 취소 후 신규 주문으로 진행합니다.")
            return self._cancel_then_place(order_uuid, side, price, volume, post_only, market)
        self._balance_cache = None
        new_uuid = ""
        if isinstance(body, dict):
            new_uuid = str(body.get("new_order_uuid") or "")
            nested = body.get("new_order")
            if not new_uuid and isinstance(nested, dict):
                new_uuid = str(nested.get("uuid") or "")
        if not new_uuid:
            raise UpbitAPIError(f"재주문 uuid 가 없습니다: {body}", retryable=True)
        order = self.get_order(new_uuid)
        if order is None:
            raise UpbitAPIError(f"재주문 조회 실패 uuid={new_uuid}", retryable=True)
        self._log_order("재주문 응답", order)
        return order

    def _cancel_then_place(
        self,
        order_uuid: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        post_only: bool,
        market: str,
    ) -> Order:
        cancelled = self.cancel(order_uuid)
        if cancelled.executed_volume > 0:
            return cancelled
        if side == "bid":
            return self.buy_limit(market, price, volume, post_only)
        return self.sell_limit(market, price, volume, post_only)

    def notify_trade_price(self, _price: Decimal) -> None:
        return None


class DryRunExchange:
    """공개 시세는 실제로 받고, 주문·잔고는 메모리에서만 움직입니다."""

    def __init__(self, cfg: Any, market_data: UpbitREST | None = None):
        self.cfg = cfg
        self.dry_run = True
        self._market = market_data
        self._orders: dict[str, Order] = {}
        self._quote_available = cfg.dry_run_quote
        self._quote_locked = Decimal("0")
        self._base_available = Decimal("0")
        self._base_locked = Decimal("0")
        self._avg = Decimal("0")
        self._trade_price = Decimal("0")

    def begin_step(self) -> None:
        return None

    def prepare(self) -> tuple[Decimal, Decimal] | None:
        return None

    def notify_trade_price(self, price: Decimal) -> None:
        self._trade_price = price
        for order in list(self._orders.values()):
            if not order.is_open or order.price is None:
                continue
            if order.side == "bid" and price <= order.price:
                self._fill_buy(order)
            elif order.side == "ask" and price >= order.price:
                self._fill_sell(order)

    def get_ticker(self, market: str) -> Ticker:
        if self._market is not None:
            return self._market.get_ticker(market)
        if self._trade_price <= 0:
            raise UpbitAPIError("모의 현재가가 없습니다.", retryable=True)
        return Ticker(market, self._trade_price)

    def get_orderbook(self, market: str) -> OrderBook:
        if self._market is None:
            raise UpbitAPIError("모의 호가 원천이 없습니다.", retryable=False)
        return self._market.get_orderbook(market)

    def get_candles(self, market: str, unit: int, count: int) -> list[Candle]:
        if self._market is None:
            raise UpbitAPIError("모의 캔들 원천이 없습니다.", retryable=False)
        return self._market.get_candles(market, unit, count)

    def get_ohlcv_page(
        self,
        market: str,
        unit: int,
        count: int = 200,
        to: str | None = None,
    ) -> list[OhlcvBar]:
        if self._market is None or not hasattr(self._market, "get_ohlcv_page"):
            raise UpbitAPIError("이 시세 원천은 캔들 백필을 지원하지 않습니다.", retryable=False)
        return self._market.get_ohlcv_page(market, unit, count, to)

    def quote_available(self, quote: str | None = None) -> Decimal:
        del quote
        return self._quote_available

    def quote_locked(self, quote: str | None = None) -> Decimal:
        del quote
        return self._quote_locked

    def base_available(self, base: str | None = None, quote: str | None = None) -> Decimal:
        del base, quote
        return self._base_available

    def base_total(self, base: str | None = None, quote: str | None = None) -> Decimal:
        del base, quote
        return self._base_available + self._base_locked

    def base_avg_buy_price(self, base: str | None = None, quote: str | None = None) -> Decimal:
        del base, quote
        return self._avg

    def _tag(self, text: str) -> str:
        logger.info("[모의] %s", text)

    def buy_limit(self, market: str, price: Decimal, volume: Decimal, post_only: bool) -> Order:
        del post_only
        notional = price * volume
        fee = notional * self.cfg.buy_fee_rate
        total = notional + fee
        if self._quote_available < total:
            raise UpbitAPIError(
                "모의 매수 잔고 부족",
                status=400,
                name="insufficient_funds_bid",
                retryable=False,
            )
        self._quote_available -= total
        self._quote_locked += total
        order = Order(
            uuid=str(uuid.uuid4()),
            identifier=_identifier("b"),
            market=market,
            side="bid",
            ord_type="limit",
            state="wait",
            price=price,
            volume=volume,
            executed_volume=Decimal("0"),
            remaining_volume=volume,
            paid_fee=Decimal("0"),
            locked_funds=total,
            created_ts=time.time(),
        )
        self._orders[order.uuid] = order
        self._tag(f"지정가 매수 접수 price={to_plain_str(price)} volume={to_plain_str(volume)} uuid={order.uuid}")
        return order

    def sell_limit(self, market: str, price: Decimal, volume: Decimal, post_only: bool) -> Order:
        del post_only
        if self._base_available < volume:
            raise UpbitAPIError(
                "모의 매도 잔고 부족",
                status=400,
                name="insufficient_funds_ask",
                retryable=False,
            )
        self._base_available -= volume
        self._base_locked += volume
        order = Order(
            uuid=str(uuid.uuid4()),
            identifier=_identifier("s"),
            market=market,
            side="ask",
            ord_type="limit",
            state="wait",
            price=price,
            volume=volume,
            executed_volume=Decimal("0"),
            remaining_volume=volume,
            paid_fee=Decimal("0"),
            locked_funds=volume,
            created_ts=time.time(),
        )
        self._orders[order.uuid] = order
        self._tag(f"지정가 매도 접수 price={to_plain_str(price)} volume={to_plain_str(volume)} uuid={order.uuid}")
        return order

    def sell_market(self, market: str, volume: Decimal) -> Order:
        if self._trade_price <= 0:
            raise UpbitAPIError("모의 시장가 매도에 현재가가 없습니다.", retryable=True)
        if self._base_available < volume:
            raise UpbitAPIError(
                "모의 시장가 매도 잔고 부족",
                status=400,
                name="insufficient_funds_ask",
                retryable=False,
            )
        raw = self._trade_price * (Decimal("1") - self.cfg.dry_run_slippage_rate)
        price = align_price(raw, "floor")
        self._base_available -= volume
        notional = price * volume
        fee = notional * self.cfg.sell_fee_rate
        self._quote_available += notional - fee
        order = Order(
            uuid=str(uuid.uuid4()),
            identifier=_identifier("m"),
            market=market,
            side="ask",
            ord_type="market",
            state="done",
            price=price,
            volume=volume,
            executed_volume=volume,
            remaining_volume=Decimal("0"),
            paid_fee=fee,
            locked_funds=Decimal("0"),
            trades=[{"price": to_plain_str(price), "volume": to_plain_str(volume), "funds": to_plain_str(notional)}],
            created_ts=time.time(),
        )
        self._orders[order.uuid] = order
        self._reduce_avg(volume)
        self._tag(
            f"시장가 매도 체결 price={to_plain_str(price)} volume={to_plain_str(volume)} fee={to_plain_str(fee)} uuid={order.uuid}"
        )
        return order

    def get_order(self, order_uuid: str) -> Order | None:
        return self._orders.get(order_uuid)

    def list_open_orders(self, market: str) -> list[Order]:
        return [order for order in self._orders.values() if order.market == market and order.is_open]

    def cancel(self, order_uuid: str) -> Order:
        order = self._orders.get(order_uuid)
        if order is None:
            raise UpbitAPIError("모의 주문이 없습니다.", status=404, name="order_not_found", retryable=False)
        if not order.is_open:
            return order
        if order.side == "bid":
            self._quote_locked -= order.locked_funds
            self._quote_available += order.locked_funds
            order.locked_funds = Decimal("0")
        else:
            release = order.remaining_volume
            self._base_locked -= release
            self._base_available += release
            order.locked_funds = Decimal("0")
        order.state = "cancel"
        order.remaining_volume = Decimal("0")
        self._tag(f"주문 취소 uuid={order.uuid} executed={to_plain_str(order.executed_volume)}")
        return order

    def cancel_and_replace_limit(
        self,
        order_uuid: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        post_only: bool,
        market: str,
    ) -> Order:
        cancelled = self.cancel(order_uuid)
        if cancelled.executed_volume > 0 and cancelled.remaining_volume == 0 and not cancelled.is_open:
            if cancelled.executed_volume >= cancelled.volume:
                return cancelled
        if cancelled.executed_volume > 0:
            return cancelled
        if side == "bid":
            return self.buy_limit(market, price, volume, post_only)
        return self.sell_limit(market, price, volume, post_only)

    def fill_buy_partial(self, order_uuid: str, volume: Decimal) -> Order:
        """테스트용: 지정가 매수를 일부만 체결합니다."""
        order = self._orders.get(order_uuid)
        if order is None or not order.is_open or order.side != "bid" or order.price is None:
            raise UpbitAPIError("부분 체결할 매수 주문이 없습니다.", retryable=False)
        volume = min(volume, order.remaining_volume)
        if volume <= 0:
            return order
        notional = order.price * volume
        fee = notional * self.cfg.buy_fee_rate
        spent = notional + fee
        self._quote_locked -= spent
        order.locked_funds -= spent
        self._credit_base(volume, order.price)
        order.executed_volume += volume
        order.remaining_volume -= volume
        order.paid_fee += fee
        order.trades.append(
            {"price": to_plain_str(order.price), "volume": to_plain_str(volume), "funds": to_plain_str(notional)}
        )
        if order.remaining_volume <= 0:
            order.remaining_volume = Decimal("0")
            order.locked_funds = Decimal("0")
            order.state = "done"
        self._tag(
            f"지정가 매수 부분체결 price={to_plain_str(order.price)} volume={to_plain_str(volume)} fee={to_plain_str(fee)} uuid={order.uuid}"
        )
        return order

    def _fill_buy(self, order: Order) -> None:
        volume = order.remaining_volume
        if volume <= 0 or order.price is None:
            return
        notional = order.price * volume
        fee = notional * self.cfg.buy_fee_rate
        self._quote_locked -= order.locked_funds
        order.locked_funds = Decimal("0")
        self._credit_base(volume, order.price)
        order.executed_volume += volume
        order.remaining_volume = Decimal("0")
        order.paid_fee += fee
        order.trades.append(
            {"price": to_plain_str(order.price), "volume": to_plain_str(volume), "funds": to_plain_str(notional)}
        )
        order.state = "done"
        self._tag(
            f"지정가 매수 체결 price={to_plain_str(order.price)} volume={to_plain_str(volume)} fee={to_plain_str(fee)} uuid={order.uuid}"
        )

    def _fill_sell(self, order: Order) -> None:
        volume = order.remaining_volume
        if volume <= 0 or order.price is None:
            return
        notional = order.price * volume
        fee = notional * self.cfg.sell_fee_rate
        self._base_locked -= volume
        order.locked_funds = Decimal("0")
        self._quote_available += notional - fee
        order.executed_volume += volume
        order.remaining_volume = Decimal("0")
        order.paid_fee += fee
        order.trades.append(
            {"price": to_plain_str(order.price), "volume": to_plain_str(volume), "funds": to_plain_str(notional)}
        )
        order.state = "done"
        self._reduce_avg(volume)
        self._tag(
            f"지정가 매도 체결 price={to_plain_str(order.price)} volume={to_plain_str(volume)} fee={to_plain_str(fee)} uuid={order.uuid}"
        )

    def _credit_base(self, volume: Decimal, price: Decimal) -> None:
        total = self._base_available + self._base_locked
        new_total = total + volume
        if new_total > 0:
            self._avg = ((self._avg * total) + (price * volume)) / new_total
        self._base_available += volume

    def _reduce_avg(self, volume: Decimal) -> None:
        total = self._base_available + self._base_locked
        if total <= 0:
            self._avg = Decimal("0")
        del volume


class ScriptedMarket:
    """테스트용 시세. 네트워크를 쓰지 않습니다."""

    def __init__(self) -> None:
        self.trade_price = Decimal("100")
        self.best_bid = Decimal("99.99")
        self.best_ask = Decimal("100.01")
        self.bid_size = Decimal("1")
        self.ask_size = Decimal("1")
        self.box_low = Decimal("99")
        self.box_high = Decimal("101")

    def get_ticker(self, market: str) -> Ticker:
        return Ticker(market, self.trade_price)

    def get_orderbook(self, market: str) -> OrderBook:
        return OrderBook(market, self.best_bid, self.best_ask, self.bid_size, self.ask_size)

    def get_candles(self, market: str, unit: int, count: int) -> list[Candle]:
        del market, unit
        candle = Candle(self.trade_price, self.box_high, self.box_low, self.trade_price)
        return [candle for _ in range(count)]


class UpbitExchange(UpbitREST):
    """실계좌 주문. 잔고 조회는 마켓 단위(USDT 로 산 BTC 와 KRW 로 산 BTC)를 구분합니다."""

    def __init__(self, cfg: Any):
        super().__init__(
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            http_timeout=cfg.http_timeout,
            max_retries=cfg.max_retries,
            retry_delay=cfg.retry_delay_sec,
            quotation_rps=cfg.quotation_rps,
            account_rps=cfg.account_rps,
            order_rps=cfg.order_rps,
        )
        self.cfg = cfg
        self.dry_run = False

    def prepare(self) -> tuple[Decimal, Decimal] | None:
        return self.fetch_fees(self.cfg.market)

    def get_ticker(self, market: str | None = None) -> Ticker:
        return super().get_ticker(market or self.cfg.market)

    def get_orderbook(self, market: str | None = None) -> OrderBook:
        return super().get_orderbook(market or self.cfg.market)

    def get_candles(self, market: str | None = None, unit: int | None = None, count: int | None = None) -> list[Candle]:
        return super().get_candles(
            market or self.cfg.market,
            self.cfg.box_candle_unit if unit is None else unit,
            self.cfg.box_candle_count if count is None else count,
        )

    def quote_available(self, quote: str | None = None) -> Decimal:
        return super().quote_available(quote or self.cfg.quote)

    def quote_locked(self, quote: str | None = None) -> Decimal:
        return super().quote_locked(quote or self.cfg.quote)

    def base_available(self, base: str | None = None, quote: str | None = None) -> Decimal:
        return super().base_available(base or self.cfg.base, quote or self.cfg.quote)

    def base_total(self, base: str | None = None, quote: str | None = None) -> Decimal:
        return super().base_total(base or self.cfg.base, quote or self.cfg.quote)

    def base_avg_buy_price(self, base: str | None = None, quote: str | None = None) -> Decimal:
        return super().base_avg_buy_price(base or self.cfg.base, quote or self.cfg.quote)

    def buy_limit(self, market: str, price: Decimal, volume: Decimal, post_only: bool | None = None) -> Order:
        return super().buy_limit(market, price, volume, self.cfg.post_only if post_only is None else post_only)

    def sell_limit(self, market: str, price: Decimal, volume: Decimal, post_only: bool | None = None) -> Order:
        return super().sell_limit(market, price, volume, self.cfg.post_only if post_only is None else post_only)

    def cancel_and_replace_limit(
        self,
        order_uuid: str,
        side: str,
        price: Decimal,
        volume: Decimal,
        post_only: bool | None = None,
        market: str | None = None,
    ) -> Order:
        return super().cancel_and_replace_limit(
            order_uuid,
            side,
            price,
            volume,
            self.cfg.post_only if post_only is None else post_only,
            market or self.cfg.market,
        )


def build_exchange(cfg: Any, market_data: Any = None) -> Any:
    if cfg.dry_run:
        source = market_data
        if source is None:
            source = UpbitREST(
                access_key="",
                secret_key="",
                http_timeout=cfg.http_timeout,
                max_retries=cfg.max_retries,
                retry_delay=cfg.retry_delay_sec,
                quotation_rps=cfg.quotation_rps,
                account_rps=cfg.account_rps,
                order_rps=cfg.order_rps,
            )
        return DryRunExchange(cfg, source)
    return UpbitExchange(cfg)
