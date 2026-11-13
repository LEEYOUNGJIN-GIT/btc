"""환경 변수에서 봇 설정을 읽습니다. 비밀키는 코드에 두지 않습니다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from strategy import StrategyParams


def _raw(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _text(name: str, default: str) -> str:
    value = _raw(name)
    return default if value is None else value


def _bool(name: str, default: bool) -> bool:
    value = _raw(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _decimal(name: str, default: str) -> Decimal:
    value = _raw(name)
    return Decimal(default if value is None else value)


def _int(name: str, default: int) -> int:
    value = _raw(name)
    return default if value is None else int(value)


@dataclass
class AppConfig:
    access_key: str
    secret_key: str
    market: str
    dry_run: bool
    live_trading_confirm: bool
    dry_run_quote: Decimal
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    target_profit_rate: Decimal
    stop_loss_rate: Decimal
    stop_loss_order_type: str
    dry_run_slippage_rate: Decimal
    balance_use_ratio: Decimal
    quote_reserve: Decimal
    min_order_quote: Decimal
    entry_zone: Decimal
    breakdown_buffer: Decimal
    buy_offset_rate: Decimal
    box_candle_unit: int
    box_candle_count: int
    candle_refresh_sec: float
    buy_timeout_sec: float
    sell_reprice_sec: float
    post_only: bool
    loop_interval_sec: float
    price_stale_sec: float
    use_websocket: bool
    http_timeout: float
    max_retries: int
    retry_delay_sec: float
    volume_precision: int
    post_stop_cooldown_sec: float
    max_daily_loss_quote: Decimal
    max_runtime_sec: float
    live_countdown_sec: int
    cancel_buy_on_exit: bool
    log_file: str
    log_level: str
    state_file: str
    health_every: int = 20
    reject_cooldown_sec: float = 2.0
    quotation_rps: float = 8.0
    account_rps: float = 15.0
    order_rps: float = 6.0
    backfill_enabled: bool = True
    backfill_hours: int = 24
    backfill_unit: int = 1
    backfill_db: str = "data/ohlcv.sqlite"
    backfill_csv: str = ""
    backfill_page_sleep: float = 0.15
    backfill_markets: tuple[str, ...] = ("USDT-BTC", "USDT-ETH")
    atr_period: int = 14
    range_lookback: int = 120
    min_grid_rate: Decimal = Decimal("0.002")
    max_grid_rate: Decimal = Decimal("0.004")
    support_percentile: Decimal = Decimal("0.10")
    resistance_percentile: Decimal = Decimal("0.90")
    analyze_refresh_sec: float = 60
    dynamic_reprice_sec: float = 15
    multi_tf_enabled: bool = True
    daily_data_dir: str = "data/daily"
    daily_sma_period: int = 20
    stop_loss_enabled: bool = False
    max_spread_rate: Decimal = Decimal("0.001")
    kill_switch_file: str = "STOP"
    flatten_file: str = "FLATTEN"
    heartbeat_file: str = "bot.heartbeat"
    max_order_quote: Decimal = Decimal("0")
    allow_no_loss_limit: bool = False
    error_alert_threshold: int = 5
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    healthcheck_url: str = ""
    strategy_mode: str = "ladder"
    ladder_step_rate: Decimal = Decimal("0.03")
    ladder_slots: int = 10
    ladder_lookback_days: int = 120
    ladder_budget_quote: Decimal = Decimal("0")
    ladder_max_open_buys: int = 3
    ladder_anchor_refresh_sec: float = 3600
    ladder_state_file: str = "ladder_state.json"
    ladder_poll_sec: float = 5
    ladder_anchor_mode: str = "price"
    ladder_reanchor_rate: Decimal = Decimal("0.005")

    @property
    def quote(self) -> str:
        return self.market.split("-")[0]

    @property
    def base(self) -> str:
        return self.market.split("-")[1]

    def to_strategy_params(self) -> StrategyParams:
        return StrategyParams(
            buy_fee_rate=self.buy_fee_rate,
            sell_fee_rate=self.sell_fee_rate,
            target_profit_rate=self.target_profit_rate,
            stop_loss_rate=self.stop_loss_rate,
            buy_offset_rate=self.buy_offset_rate,
            entry_zone=self.entry_zone,
            breakdown_buffer=self.breakdown_buffer,
            min_order_quote=self.min_order_quote,
            balance_use_ratio=self.balance_use_ratio,
            quote_reserve=self.quote_reserve,
            volume_precision=self.volume_precision,
        )

    def validate(self) -> None:
        parts = self.market.split("-")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("MARKET 은 USDT-BTC 처럼 QUOTE-BASE 형식이어야 합니다.")
        if self.quote != "USDT":
            raise ValueError("이 봇의 호가 단위·최소금액 규칙은 USDT 마켓 기준입니다.")
        if self.strategy_mode not in {"ladder", "scalp"}:
            raise ValueError("STRATEGY_MODE 는 ladder 또는 scalp 입니다.")
        if self.ladder_anchor_mode not in {"price", "high"}:
            raise ValueError("LADDER_ANCHOR_MODE 는 price 또는 high 입니다.")
        if self.ladder_reanchor_rate < 0:
            raise ValueError("LADDER_REANCHOR_RATE 는 0 이상이어야 합니다.")
        if self.ladder_max_open_buys < 1:
            raise ValueError("LADDER_MAX_OPEN_BUYS 는 1 이상이어야 합니다.")
        if self.stop_loss_order_type not in {"market", "limit"}:
            raise ValueError("STOP_LOSS_ORDER_TYPE 은 market 또는 limit 입니다.")
        if self.box_candle_unit not in {1, 3, 5, 10, 15, 30, 60, 240}:
            raise ValueError("BOX_CANDLE_UNIT 이 업비트 분봉 단위가 아닙니다.")
        if not 1 <= self.box_candle_count <= 200:
            raise ValueError("BOX_CANDLE_COUNT 는 1~200 입니다.")
        if self.volume_precision < 0 or self.volume_precision > 8:
            raise ValueError("VOLUME_PRECISION 은 0~8 입니다.")
        if self.max_retries < 1:
            raise ValueError("MAX_RETRIES 는 1 이상이어야 합니다.")
        if self.loop_interval_sec <= 0:
            raise ValueError("LOOP_INTERVAL_SEC 는 0보다 커야 합니다.")
        if not 1 <= self.backfill_hours <= 24 * 14:
            raise ValueError("BACKFILL_HOURS 는 1~336 입니다.")
        if self.backfill_unit not in {1, 3, 5, 10, 15, 30, 60, 240}:
            raise ValueError("BACKFILL_UNIT 이 업비트 분봉 단위가 아닙니다.")
        if self.atr_period < 2 or self.range_lookback < self.atr_period:
            raise ValueError("RANGE_LOOKBACK 은 ATR_PERIOD 이상이어야 합니다.")
        if not Decimal("0") < self.min_grid_rate < self.max_grid_rate < 1:
            raise ValueError("MIN_GRID_RATE 와 MAX_GRID_RATE 범위를 확인하세요.")
        if not Decimal("0") <= self.support_percentile < self.resistance_percentile <= 1:
            raise ValueError("지지/저항 백분위가 올바르지 않습니다.")
        if self.analyze_refresh_sec <= 0 or self.dynamic_reprice_sec < 0:
            raise ValueError("분석 갱신 주기가 올바르지 않습니다.")
        if self.backfill_page_sleep < 0:
            raise ValueError("BACKFILL_PAGE_SLEEP 는 0 이상이어야 합니다.")
        if self.daily_sma_period < 2:
            raise ValueError("DAILY_SMA_PERIOD 는 2 이상이어야 합니다.")
        if not self.backfill_markets:
            raise ValueError("BACKFILL_MARKETS 가 비어 있습니다.")
        for market in self.backfill_markets:
            parts = market.split("-")
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise ValueError(f"BACKFILL_MARKETS 항목이 올바르지 않습니다: {market}")
            if parts[0] != "USDT":
                raise ValueError("BACKFILL_MARKETS 는 USDT 마켓만 지원합니다.")
        if self.market not in self.backfill_markets:
            raise ValueError("MARKET 은 BACKFILL_MARKETS 에 포함되어야 합니다.")
        StrategyParams(
            buy_fee_rate=self.buy_fee_rate,
            sell_fee_rate=self.sell_fee_rate,
            target_profit_rate=self.target_profit_rate,
            stop_loss_rate=self.stop_loss_rate,
            buy_offset_rate=self.buy_offset_rate,
            entry_zone=self.entry_zone,
            breakdown_buffer=self.breakdown_buffer,
            min_order_quote=self.min_order_quote,
            balance_use_ratio=self.balance_use_ratio,
            quote_reserve=self.quote_reserve,
            volume_precision=self.volume_precision,
        ).validate()
        if not self.dry_run:
            if not self.access_key or not self.secret_key:
                raise ValueError("실전 모드에는 UPBIT_ACCESS_KEY 와 UPBIT_SECRET_KEY 가 필요합니다.")
            if not self.live_trading_confirm:
                raise ValueError("실전 주문은 LIVE_TRADING_CONFIRM=YES 일 때만 허용됩니다.")


def validate_live_safety(cfg: "AppConfig") -> None:
    """실전 모드 전용 안전 규칙. main() 에서만 호출합니다(기존 테스트 설정과 분리)."""
    if cfg.max_order_quote < 0:
        raise ValueError("MAX_ORDER_QUOTE 는 0 이상이어야 합니다(0 은 제한 없음).")
    if cfg.error_alert_threshold < 1:
        raise ValueError("ERROR_ALERT_THRESHOLD 는 1 이상이어야 합니다.")
    if bool(cfg.telegram_bot_token) != bool(cfg.telegram_chat_id):
        raise ValueError("TELEGRAM_BOT_TOKEN 과 TELEGRAM_CHAT_ID 는 함께 설정하거나 함께 비워야 합니다.")
    if cfg.dry_run:
        return
    if cfg.strategy_mode == "ladder":
        # 분할 구간 모드는 손절이 없어 일일 손실 한도가 작동하지 않습니다. 투입 총액이 상한입니다.
        if cfg.ladder_budget_quote <= 0:
            raise ValueError("실전 분할 구간 모드에는 LADDER_BUDGET_QUOTE(투입할 USDT 총액)가 0보다 커야 합니다.")
        return
    if cfg.max_daily_loss_quote <= 0 and not cfg.allow_no_loss_limit:
        raise ValueError(
            "실전 모드에는 MAX_DAILY_LOSS_QUOTE(일일 최대 손실, USDT)가 0보다 커야 합니다. "
            "의도적으로 끄려면 ALLOW_NO_LOSS_LIMIT=true 를 명시하세요."
        )


def _markets(name: str, default: str) -> tuple[str, ...]:
    value = _text(name, default)
    markets: list[str] = []
    seen: set[str] = set()
    for part in value.replace(";", ",").split(","):
        market = part.strip().upper()
        if not market or market in seen:
            continue
        seen.add(market)
        markets.append(market)
    return tuple(markets)


def load_config() -> AppConfig:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    load_dotenv()
    market = _text("MARKET", "USDT-BTC").upper()
    backfill_markets = _markets("BACKFILL_MARKETS", "USDT-BTC,USDT-ETH")
    if market not in backfill_markets:
        backfill_markets = (market, *backfill_markets)
    config = AppConfig(
        access_key=_text("UPBIT_ACCESS_KEY", ""),
        secret_key=_text("UPBIT_SECRET_KEY", ""),
        market=market,
        dry_run=_bool("DRY_RUN", True),
        live_trading_confirm=_text("LIVE_TRADING_CONFIRM", "NO").upper() == "YES",
        dry_run_quote=_decimal("DRY_RUN_QUOTE", "1000"),
        buy_fee_rate=_decimal("BUY_FEE_RATE", "0.0025"),
        sell_fee_rate=_decimal("SELL_FEE_RATE", "0.0025"),
        target_profit_rate=_decimal("TARGET_PROFIT_RATE", "0.0025"),
        stop_loss_rate=_decimal("STOP_LOSS_RATE", "0.005"),
        stop_loss_order_type=_text("STOP_LOSS_ORDER_TYPE", "market").lower(),
        dry_run_slippage_rate=_decimal("DRY_RUN_SLIPPAGE_RATE", "0.0005"),
        balance_use_ratio=_decimal("BALANCE_USE_RATIO", "1"),
        quote_reserve=_decimal("QUOTE_RESERVE", "0"),
        min_order_quote=_decimal("MIN_ORDER_QUOTE", "0.5"),
        entry_zone=_decimal("ENTRY_ZONE", "0.40"),
        breakdown_buffer=_decimal("BREAKDOWN_BUFFER", "0.002"),
        buy_offset_rate=_decimal("BUY_OFFSET_RATE", "0.001"),
        box_candle_unit=_int("BOX_CANDLE_UNIT", 1),
        box_candle_count=_int("BOX_CANDLE_COUNT", 30),
        candle_refresh_sec=float(_text("CANDLE_REFRESH_SEC", "30")),
        buy_timeout_sec=float(_text("BUY_TIMEOUT_SEC", "45")),
        sell_reprice_sec=float(_text("SELL_REPRICE_SEC", "120")),
        post_only=_bool("POST_ONLY", False),
        loop_interval_sec=float(_text("LOOP_INTERVAL_SEC", "1")),
        price_stale_sec=float(_text("PRICE_STALE_SEC", "5")),
        use_websocket=_bool("USE_WEBSOCKET", True),
        http_timeout=float(_text("ORDER_TIMEOUT_HTTP", "10")),
        max_retries=_int("MAX_RETRIES", 5),
        retry_delay_sec=float(_text("RETRY_DELAY_SEC", "1")),
        volume_precision=_int("VOLUME_PRECISION", 8),
        post_stop_cooldown_sec=float(_text("POST_STOP_COOLDOWN_SEC", "15")),
        max_daily_loss_quote=_decimal("MAX_DAILY_LOSS_QUOTE", "0"),
        max_runtime_sec=float(_text("MAX_RUNTIME_SEC", "0")),
        live_countdown_sec=_int("LIVE_COUNTDOWN_SEC", 5),
        cancel_buy_on_exit=_bool("CANCEL_BUY_ON_EXIT", True),
        log_file=_text("LOG_FILE", "trading.log"),
        log_level=_text("LOG_LEVEL", "INFO").upper(),
        state_file=_text("STATE_FILE", "position_state.json"),
        backfill_enabled=_bool("BACKFILL_ENABLED", True),
        backfill_hours=_int("BACKFILL_HOURS", 24),
        backfill_unit=_int("BACKFILL_UNIT", 1),
        backfill_db=_text("BACKFILL_DB", "data/ohlcv.sqlite"),
        backfill_csv=_text("BACKFILL_CSV", ""),
        backfill_page_sleep=float(_text("BACKFILL_PAGE_SLEEP", "0.15")),
        backfill_markets=backfill_markets,
        atr_period=_int("ATR_PERIOD", 14),
        range_lookback=_int("RANGE_LOOKBACK", 120),
        min_grid_rate=_decimal("MIN_GRID_RATE", "0.002"),
        max_grid_rate=_decimal("MAX_GRID_RATE", "0.004"),
        support_percentile=_decimal("SUPPORT_PERCENTILE", "0.10"),
        resistance_percentile=_decimal("RESISTANCE_PERCENTILE", "0.90"),
        analyze_refresh_sec=float(_text("ANALYZE_REFRESH_SEC", "60")),
        dynamic_reprice_sec=float(_text("DYNAMIC_REPRICE_SEC", "15")),
        multi_tf_enabled=_bool("MULTI_TF_ENABLED", True),
        daily_data_dir=_text("DAILY_DATA_DIR", "data/daily"),
        daily_sma_period=_int("DAILY_SMA_PERIOD", 20),
        stop_loss_enabled=_bool("STOP_LOSS_ENABLED", False),
        max_spread_rate=_decimal("MAX_SPREAD_RATE", "0.001"),
        kill_switch_file=_text("KILL_SWITCH_FILE", "STOP"),
        flatten_file=_text("FLATTEN_FILE", "FLATTEN"),
        heartbeat_file=_text("HEARTBEAT_FILE", "bot.heartbeat"),
        max_order_quote=_decimal("MAX_ORDER_QUOTE", "0"),
        allow_no_loss_limit=_bool("ALLOW_NO_LOSS_LIMIT", False),
        error_alert_threshold=_int("ERROR_ALERT_THRESHOLD", 5),
        telegram_bot_token=_text("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=_text("TELEGRAM_CHAT_ID", ""),
        healthcheck_url=_text("HEALTHCHECK_URL", ""),
        strategy_mode=_text("STRATEGY_MODE", "ladder").lower(),
        ladder_step_rate=_decimal("LADDER_STEP_RATE", "0.03"),
        ladder_slots=_int("LADDER_SLOTS", 10),
        ladder_lookback_days=_int("LADDER_LOOKBACK_DAYS", 120),
        ladder_budget_quote=_decimal("LADDER_BUDGET_QUOTE", "0"),
        ladder_max_open_buys=_int("LADDER_MAX_OPEN_BUYS", 3),
        ladder_anchor_refresh_sec=float(_text("LADDER_ANCHOR_REFRESH_SEC", "3600")),
        ladder_state_file=_text("LADDER_STATE_FILE", "ladder_state.json"),
        ladder_poll_sec=float(_text("LADDER_POLL_SEC", "5")),
        ladder_anchor_mode=_text("LADDER_ANCHOR_MODE", "price").lower(),
        ladder_reanchor_rate=_decimal("LADDER_REANCHOR_RATE", "0.005"),
    )
    config.validate()
    return config
