"""실전 투입 전 점검 CLI.

    python preflight.py                      # IP 확인 + 키/권한/수수료 점검 (주문 없음)
    python preflight.py --order-test --yes   # 체결될 수 없는 소액 지정가 매수를 넣었다 바로 취소 (실제 주문 발생)

순서가 중요합니다. 업비트는 키를 발급할 때 허용 IP 를 먼저 정해야 하므로,
(1) 이 서버에서 먼저 `python preflight.py --ip-only` 로 공개 IP 를 확인하고
(2) 그 IP 를 허용 IP 로 넣어 키를 발급한 뒤
(3) .env 에 키를 넣고 전체 점검을 실행합니다.
"""

from __future__ import annotations

import argparse
import sys
from decimal import ROUND_UP, Decimal
from typing import Any, Callable

import requests

from config import AppConfig, load_config, validate_live_safety
from exchange_api import UpbitAPIError, UpbitREST
from hints import explain_upbit_error
from strategy import align_price, to_plain_str

IP_ENDPOINTS = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")
OK = "[통과]"
FAIL = "[실패]"
WARN = "[주의]"


def discover_public_ip(get: Callable[..., Any] = requests.get) -> str | None:
    for url in IP_ENDPOINTS:
        try:
            response = get(url, timeout=5)
            text = str(response.text).strip()
            if response.status_code == 200 and 7 <= len(text) <= 45 and " " not in text:
                return text
        except Exception:
            continue
    return None


def build_rest(cfg: AppConfig) -> UpbitREST:
    return UpbitREST(
        access_key=cfg.access_key,
        secret_key=cfg.secret_key,
        http_timeout=cfg.http_timeout,
        max_retries=1,
        retry_delay=cfg.retry_delay_sec,
        quotation_rps=cfg.quotation_rps,
        account_rps=cfg.account_rps,
        order_rps=cfg.order_rps,
    )


def check_accounts(rest: UpbitREST, cfg: AppConfig) -> tuple[bool, str]:
    try:
        body = rest._request("GET", "/v1/accounts", None, "default", auth=True)
    except UpbitAPIError as exc:
        return False, explain_upbit_error(exc.name, str(exc))
    except requests.RequestException as exc:
        return False, f"네트워크 오류: {type(exc).__name__}"
    if not isinstance(body, list):
        return False, f"응답 형식이 예상과 다릅니다: {str(body)[:120]}"
    usdt = Decimal("0")
    for row in body:
        if str(row.get("currency")) == cfg.quote:
            usdt = Decimal(str(row.get("balance", "0")))
    return True, f"자산조회 성공. {cfg.quote} 잔고 {to_plain_str(usdt)}" + describe_base_rows(body, cfg)


def describe_base_rows(rows: list[Any], cfg: AppConfig) -> str:
    """거래 대상 코인(BTC 등)이 어떤 단위(unit_currency)의 행으로 잡혀 있는지 보여줍니다.

    봇은 unit_currency 가 마켓의 호가 통화(USDT)인 행만 자기 잔고로 봅니다. 원화로 이미 보유한 장기
    보유분은 건드리지 않기 위한 선택인데, 업비트가 USDT 마켓 매수분을 어떤 행으로 돌려주는지는
    실계좌 응답으로 확인되지 않았습니다. 그래서 응답을 그대로 보여줍니다(키 값은 포함되지 않습니다).
    """
    own = [r for r in rows if str(r.get("currency")) == cfg.base]
    if not own:
        return f" | {cfg.base} 보유 행 없음"
    parts = [
        f"{r.get('currency')}[unit={r.get('unit_currency')}] 수량 {r.get('balance')}(+잠김 {r.get('locked')}) 평단 {r.get('avg_buy_price')}"
        for r in own
    ]
    note = " | " + "; ".join(parts)
    foreign = [r for r in own if str(r.get("unit_currency")) != cfg.quote and Decimal(str(r.get("balance", "0"))) > 0]
    if foreign:
        note += (
            f" | 주의: {cfg.quote} 단위가 아닌 {cfg.base} 보유분이 있습니다. 봇은 이 수량을 자기 포지션으로 "
            f"보지 않습니다. 단, USDT 마켓에서 산 {cfg.base} 가 이 행에 합쳐져 보이면 봇은 매수 체결분을 찾지 못할 수 있으니 "
            "소액으로 먼저 확인하세요."
        )
    return note


def check_chance(rest: UpbitREST, cfg: AppConfig) -> tuple[bool, str]:
    try:
        body = rest._request("GET", "/v1/orders/chance", {"market": cfg.market}, "default", auth=True)
    except UpbitAPIError as exc:
        return False, explain_upbit_error(exc.name, str(exc))
    except requests.RequestException as exc:
        return False, f"네트워크 오류: {type(exc).__name__}"
    if not isinstance(body, dict):
        return False, "주문 가능 정보 응답 형식이 예상과 다릅니다."
    bid_fee, ask_fee = body.get("bid_fee"), body.get("ask_fee")
    detail = f"수수료 매수 {bid_fee} / 매도 {ask_fee}"
    market = body.get("market") or {}
    bid = market.get("bid") or {}
    if bid.get("min_total"):
        detail += f" | 매수 최소주문 {bid.get('min_total')} {bid.get('currency', '')}".rstrip()
        try:
            if Decimal(str(bid["min_total"])) > cfg.min_order_quote:
                detail += f" (설정 MIN_ORDER_QUOTE {cfg.min_order_quote} 보다 큼. 값을 올리세요)"
        except Exception:
            pass
    return True, detail


def order_test(rest: UpbitREST, cfg: AppConfig) -> tuple[bool, str]:
    """시장가의 절반 가격 지정가 매수를 넣고 즉시 취소합니다. 체결될 수 없는 가격이지만 실제 주문입니다."""
    book = rest.get_orderbook(cfg.market)
    price = align_price(book.best_bid * Decimal("0.5"), "floor")
    volume = (max(cfg.min_order_quote, Decimal("0.5")) * Decimal("1.5") / price).quantize(
        Decimal(1).scaleb(-cfg.volume_precision), rounding=ROUND_UP
    )
    order = None
    try:
        order = rest.buy_limit(cfg.market, price, volume, False)
        cancelled = rest.cancel(order.uuid)
    except UpbitAPIError as exc:
        if order is not None:
            return False, f"주문은 접수됐으나 취소 실패. 거래소에서 직접 확인하세요. uuid={order.uuid} ({exc})"
        return False, explain_upbit_error(exc.name, str(exc))
    return True, (
        f"주문하기/취소 권한 확인. price={to_plain_str(price)} volume={to_plain_str(volume)} "
        f"최종상태={cancelled.state}"
    )


def run(argv: list[str] | None = None, *, get: Callable[..., Any] = requests.get) -> int:
    parser = argparse.ArgumentParser(description="실전 투입 전 점검")
    parser.add_argument("--ip-only", action="store_true", help="공개 IP 만 확인(키 불필요)")
    parser.add_argument("--order-test", action="store_true", help="소액 지정가 주문 후 취소로 주문 권한 확인")
    parser.add_argument("--yes", action="store_true", help="--order-test 가 실제 주문을 넣는다는 점에 동의")
    args = parser.parse_args(argv)

    ip = discover_public_ip(get)
    if ip:
        print(f"이 서버의 공개 IP: {ip}")
        print("  업비트 Open API 관리의 '허용 IP' 에 이 값을 등록하세요. 서버 IP 가 바뀌면 다시 등록해야 합니다.")
    else:
        print(f"{WARN} 공개 IP 를 확인하지 못했습니다. 네트워크/방화벽을 확인하세요.")
    if args.ip_only:
        return 0 if ip else 1

    try:
        cfg = load_config()
    except ValueError as exc:
        print(f"{FAIL} 설정 오류: {exc}")
        return 1
    failed = False
    if not cfg.access_key or not cfg.secret_key:
        print(f"{FAIL} .env 에 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 가 없습니다.")
        return 1
    try:
        validate_live_safety(cfg)
        print(f"{OK} 안전 설정 (일일 손실 한도 {to_plain_str(cfg.max_daily_loss_quote)}, 1회 주문 상한 {to_plain_str(cfg.max_order_quote)})")
    except ValueError as exc:
        print(f"{WARN} 실전 실행 전에 해결 필요: {exc}")
    if cfg.dry_run:
        print(f"{WARN} 현재 DRY_RUN=true 입니다. 점검은 가능하지만 실전 가동에는 DRY_RUN=false 가 필요합니다.")

    rest = build_rest(cfg)
    ok, msg = check_accounts(rest, cfg)
    print(f"{OK if ok else FAIL} 자산조회: {msg}")
    failed |= not ok
    if ok:
        ok, msg = check_chance(rest, cfg)
        print(f"{OK if ok else FAIL} 주문가능정보: {msg}")
        failed |= not ok
    if args.order_test:
        if not args.yes:
            print(f"{FAIL} --order-test 는 실제 주문을 넣습니다. 동의하면 --yes 를 함께 주세요.")
            return 1
        if failed:
            print(f"{WARN} 앞 단계가 실패해 주문 테스트를 건너뜁니다.")
        else:
            ok, msg = order_test(rest, cfg)
            print(f"{OK if ok else FAIL} 주문 권한: {msg}")
            failed |= not ok
    else:
        print("주문 권한은 아직 검증되지 않았습니다. --order-test --yes 로 확인하세요(체결 불가 가격의 소액 지정가 후 즉시 취소).")
    print("점검 완료." if not failed else "점검 실패 항목이 있습니다.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
