import unittest
from decimal import Decimal

from config import AppConfig
from exchange_api import DryRunExchange, NullFeed, ScriptedMarket
from main import RoundTripBot
from strategy import Strategy


def make_config(**overrides: object) -> AppConfig:
    values: dict[str, object] = dict(
        access_key="",
        secret_key="",
        market="USDT-BTC",
        dry_run=True,
        live_trading_confirm=False,
        dry_run_quote=Decimal("1000"),
        buy_fee_rate=Decimal("0.0005"),
        sell_fee_rate=Decimal("0.0005"),
        target_profit_rate=Decimal("0.0025"),
        stop_loss_rate=Decimal("0.005"),
        stop_loss_order_type="market",
        dry_run_slippage_rate=Decimal("0.0005"),
        balance_use_ratio=Decimal("1"),
        quote_reserve=Decimal("0"),
        min_order_quote=Decimal("0.5"),
        entry_zone=Decimal("0.40"),
        breakdown_buffer=Decimal("0.002"),
        buy_offset_rate=Decimal("0.001"),
        box_candle_unit=1,
        box_candle_count=10,
        candle_refresh_sec=30,
        buy_timeout_sec=45,
        sell_reprice_sec=120,
        post_only=False,
        loop_interval_sec=0.01,
        price_stale_sec=5,
        use_websocket=False,
        http_timeout=5,
        max_retries=2,
        retry_delay_sec=0.01,
        volume_precision=8,
        post_stop_cooldown_sec=30,
        max_daily_loss_quote=Decimal("0"),
        max_runtime_sec=0,
        live_countdown_sec=0,
        cancel_buy_on_exit=True,
        log_file="trading.log",
        log_level="WARNING",
        state_file="position_state.json",
    )
    values.update(overrides)
    config = AppConfig(**values)  # type: ignore[arg-type]
    config.validate()
    return config


class BotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = make_config()
        self.market = ScriptedMarket()
        self.market.trade_price = Decimal("99.20")
        self.market.best_bid = Decimal("99.20")
        self.market.best_ask = Decimal("99.22")
        self.market.box_low = Decimal("99")
        self.market.box_high = Decimal("101")
        self.exchange = DryRunExchange(self.cfg, self.market)
        self.bot = RoundTripBot(self.cfg, self.exchange, Strategy(self.cfg.to_strategy_params()), NullFeed())

    def _enter_sell_wait(self) -> None:
        self.bot.step()
        self.assertEqual(self.bot.phase, "BUY_WAIT")
        order = self.exchange.get_order(self.bot.order_uuid or "")
        assert order is not None and order.price is not None
        self.market.trade_price = order.price
        self.market.best_bid = order.price
        self.market.best_ask = order.price + Decimal("0.01")
        self.bot.step()
        self.assertEqual(self.bot.phase, "SELL_WAIT")

    def test_round_trip_increases_quote(self) -> None:
        self._enter_sell_wait()
        buy_price = self.bot.position.buy_price if self.bot.position else Decimal("0")
        sell = self.exchange.get_order(self.bot.order_uuid or "")
        assert sell is not None and sell.price is not None
        self.assertGreaterEqual(sell.price, self.bot.strategy.min_sell_price(buy_price))
        self.market.trade_price = sell.price
        self.market.best_bid = sell.price - Decimal("0.01")
        self.market.best_ask = sell.price
        self.bot.step()
        self.assertEqual(self.bot.phase, "FLAT")
        self.assertGreater(self.exchange.quote_available(), Decimal("1000"))
        self.assertEqual(self.bot.completed_cycles, 1)

    def test_stop_loss_reduces_quote_and_cools_down(self) -> None:
        self._enter_sell_wait()
        assert self.bot.position is not None
        self.market.trade_price = self.bot.position.stop_price
        self.market.best_bid = self.bot.position.stop_price
        self.market.best_ask = self.bot.position.stop_price + Decimal("0.01")
        self.bot.step()
        self.assertEqual(self.bot.phase, "FLAT")
        self.assertLess(self.exchange.quote_available(), Decimal("1000"))
        self.assertEqual(self.exchange.base_total(), Decimal("0"))
        self.bot.step()
        self.assertEqual(self.bot.phase, "FLAT")
        self.assertEqual(self.exchange.list_open_orders(self.cfg.market), [])

    def test_unfilled_buy_is_repriced(self) -> None:
        self.bot.step()
        first = self.bot.order_uuid
        first_order = self.exchange.get_order(first or "")
        assert first_order is not None and first_order.price is not None
        self.bot.order_placed_at = 0
        self.market.best_bid = Decimal("99.50")
        self.market.best_ask = Decimal("99.52")
        self.market.trade_price = Decimal("99.50")
        self.bot.step()
        self.assertEqual(self.bot.phase, "BUY_WAIT")
        self.assertNotEqual(self.bot.order_uuid, first)
        replaced = self.exchange.get_order(self.bot.order_uuid or "")
        assert replaced is not None and replaced.price is not None
        self.assertGreater(replaced.price, first_order.price)
        self.assertEqual(len(self.exchange.list_open_orders(self.cfg.market)), 1)

    def test_reconcile_open_buy_and_naked_base(self) -> None:
        order = self.exchange.buy_limit(self.cfg.market, Decimal("99"), Decimal("0.2"), False)
        self.bot._reconcile()
        self.assertEqual(self.bot.phase, "BUY_WAIT")
        self.assertEqual(self.bot.order_uuid, order.uuid)

        fresh_market = ScriptedMarket()
        fresh_market.trade_price = Decimal("100")
        fresh = DryRunExchange(self.cfg, fresh_market)
        filled = fresh.buy_limit(self.cfg.market, Decimal("100"), Decimal("0.1"), False)
        fresh.notify_trade_price(Decimal("100"))
        self.assertFalse(filled.is_open)
        resumed = RoundTripBot(self.cfg, fresh, Strategy(self.cfg.to_strategy_params()), NullFeed())
        resumed._reconcile()
        self.assertEqual(resumed.phase, "SELL_WAIT")
        sell = fresh.get_order(resumed.order_uuid or "")
        assert sell is not None and sell.price is not None
        self.assertGreaterEqual(sell.price, resumed.strategy.min_sell_price(Decimal("100")))

    def test_live_mode_requires_explicit_confirm(self) -> None:
        with self.assertRaises(ValueError):
            make_config(dry_run=False, access_key="a", secret_key="b", live_trading_confirm=False)
        with self.assertRaises(ValueError):
            make_config(market="KRW-BTC")


if __name__ == "__main__":
    unittest.main()
