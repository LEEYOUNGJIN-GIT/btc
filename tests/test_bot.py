import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

from analyzer import DynamicLevels
from backfill import OhlcvBar
from config import AppConfig
from exchange_api import DryRunExchange, NullFeed, ScriptedMarket
from main import Position, RoundTripBot
from strategy import Strategy, floor_to_precision


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
        stop_loss_enabled=False,
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
        backfill_enabled=False,
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

    def test_stop_loss_disabled_keeps_sell_wait(self) -> None:
        self._enter_sell_wait()
        assert self.bot.position is not None
        self.assertEqual(self.bot.position.stop_price, Decimal("0"))
        self.market.trade_price = Decimal("1")
        self.market.best_bid = Decimal("1")
        self.market.best_ask = Decimal("1.01")
        self.bot.step()
        self.assertEqual(self.bot.phase, "SELL_WAIT")
        self.assertGreater(self.exchange.base_total(), Decimal("0"))

    def test_stop_loss_enabled_reduces_quote_and_cools_down(self) -> None:
        self.cfg = make_config(stop_loss_enabled=True)
        self.exchange = DryRunExchange(self.cfg, self.market)
        self.bot = RoundTripBot(self.cfg, self.exchange, Strategy(self.cfg.to_strategy_params()), NullFeed())
        self._enter_sell_wait()
        assert self.bot.position is not None
        self.assertGreater(self.bot.position.stop_price, Decimal("0"))
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

    def test_reconcile_open_buy_and_suppresses_naked_base(self) -> None:
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
        self.assertEqual(resumed.phase, "HALT")
        self.assertIsNone(resumed.order_uuid)
        self.assertEqual(fresh.base_total(), Decimal("0.1"))
        self.assertEqual(fresh.list_open_orders(self.cfg.market), [])

    def test_live_mode_requires_explicit_confirm(self) -> None:
        with self.assertRaises(ValueError):
            make_config(dry_run=False, access_key="a", secret_key="b", live_trading_confirm=False)
        with self.assertRaises(ValueError):
            make_config(market="KRW-BTC")

    def test_zero_price_does_not_stop_out(self) -> None:
        self._enter_sell_wait()
        self.market.trade_price = Decimal("0")
        self.market.best_bid = Decimal("99")
        self.market.best_ask = Decimal("99.02")
        self.bot.step()
        self.assertEqual(self.bot.phase, "SELL_WAIT")
        self.assertGreater(self.exchange.base_total(), Decimal("0"))

    def test_unknown_cost_sell_is_not_repriced_or_stopped(self) -> None:
        self.exchange._base_available = Decimal("0.2")
        self.exchange._avg = Decimal("0")
        order = self.exchange.sell_limit(self.cfg.market, Decimal("100"), Decimal("0.2"), False)
        self.bot.strategy.update_levels(
            DynamicLevels(
                support=Decimal("99"),
                resistance=Decimal("101"),
                grid_rate=Decimal("0.003"),
                atr=Decimal("1"),
                atr_rate=Decimal("0.01"),
                mean_range=Decimal("1"),
                mean_range_rate=Decimal("0.01"),
                round_trip_fee_rate=Decimal("0.001"),
                sample_size=20,
                last_close=Decimal("100"),
            )
        )
        self.bot._reconcile()
        self.assertEqual(self.bot.phase, "SELL_WAIT")
        assert self.bot.position is not None
        self.assertFalse(self.bot.position.cost_known)
        self.assertEqual(self.bot.position.stop_price, Decimal("0"))
        self.assertEqual(self.bot.position.min_sell_price, Decimal("100"))
        self.bot.order_placed_at = 0
        self.market.trade_price = Decimal("50")
        self.market.best_bid = Decimal("49.99")
        self.market.best_ask = Decimal("50.01")
        self.bot.step()
        self.assertEqual(self.bot.phase, "SELL_WAIT")
        resting = self.exchange.get_order(order.uuid)
        assert resting is not None
        self.assertTrue(resting.is_open)
        self.assertEqual(resting.price, Decimal("100"))
        self.assertEqual(self.bot.order_uuid, order.uuid)
        self.exchange.cancel(order.uuid)
        self.bot.step()
        self.assertEqual(self.bot.phase, "HALT")
        self.assertEqual(self.exchange.base_total(), Decimal("0.2"))
        self.assertEqual(self.exchange.quote_available(), Decimal("1000"))

    def test_saved_partial_fill_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cfg = make_config(
                dry_run=False,
                access_key="a",
                secret_key="b",
                live_trading_confirm=True,
                state_file=str(Path(folder) / "state.json"),
            )
            exchange = DryRunExchange(cfg, self.market)
            exchange._base_available = Decimal("0.2")
            bot = RoundTripBot(cfg, exchange, Strategy(cfg.to_strategy_params()), NullFeed())
            bot.position = Position(
                cycle_id="abc",
                buy_price=Decimal("100"),
                volume=Decimal("0.3"),
                buy_notional=Decimal("30"),
                buy_fee=Decimal("0.015"),
                stop_price=Decimal("99.50"),
                min_sell_price=Decimal("100.36"),
                sold_volume=Decimal("0.1"),
                sold_funds=Decimal("12.5"),
                sold_fee=Decimal("0.006"),
                cost_known=True,
            )
            bot._save()
            order = exchange.sell_limit(cfg.market, Decimal("100.36"), Decimal("0.2"), False)
            resumed = RoundTripBot(cfg, exchange, Strategy(cfg.to_strategy_params()), NullFeed())
            resumed._reconcile()
            self.assertEqual(resumed.phase, "SELL_WAIT")
            self.assertEqual(resumed.order_uuid, order.uuid)
            assert resumed.position is not None
            self.assertEqual(resumed.position.sold_funds, Decimal("12.5"))
            self.assertEqual(resumed.position.sold_fee, Decimal("0.006"))
            self.assertTrue(resumed.position.cost_known)

    def test_daily_loss_and_cooldown_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            state = str(Path(folder) / "state.json")
            cfg = make_config(
                dry_run=False,
                access_key="a",
                secret_key="b",
                live_trading_confirm=True,
                max_daily_loss_quote=Decimal("5"),
                state_file=state,
            )
            bot = RoundTripBot(cfg, self.exchange, Strategy(cfg.to_strategy_params()), NullFeed())
            bot.realized = Decimal("-5")
            bot.completed_cycles = 2
            bot.cooldown_until = time.time() + 100
            bot._save()

            resumed = RoundTripBot(cfg, self.exchange, Strategy(cfg.to_strategy_params()), NullFeed())
            resumed._restore_bookkeeping()
            self.assertEqual(resumed.realized, Decimal("-5"))
            self.assertEqual(resumed.completed_cycles, 2)
            self.assertGreater(resumed.cooldown_until, time.time())
            resumed.step()
            self.assertEqual(resumed.phase, "HALT")
            self.assertEqual(self.exchange.list_open_orders(cfg.market), [])

            yesterday = make_config(
                dry_run=False,
                access_key="a",
                secret_key="b",
                live_trading_confirm=True,
                max_daily_loss_quote=Decimal("5"),
                state_file=state,
            )
            aged = RoundTripBot(yesterday, self.exchange, Strategy(yesterday.to_strategy_params()), NullFeed())
            aged._pnl_date = aged._pnl_date.replace(year=aged._pnl_date.year - 1)
            aged.realized = Decimal("-5")
            aged._save()
            fresh = RoundTripBot(yesterday, self.exchange, Strategy(yesterday.to_strategy_params()), NullFeed())
            fresh._restore_bookkeeping()
            self.assertEqual(fresh.realized, Decimal("0"))

            cooldown_state = str(Path(folder) / "cooldown.json")
            quiet = make_config(
                dry_run=False,
                access_key="a",
                secret_key="b",
                live_trading_confirm=True,
                max_daily_loss_quote=Decimal("0"),
                state_file=cooldown_state,
            )
            market = ScriptedMarket()
            market.trade_price = Decimal("99.20")
            market.best_bid = Decimal("99.20")
            market.best_ask = Decimal("99.22")
            market.box_low = Decimal("99")
            market.box_high = Decimal("101")
            book = DryRunExchange(quiet, market)
            waiting = RoundTripBot(quiet, book, Strategy(quiet.to_strategy_params()), NullFeed())
            waiting.cooldown_until = time.time() + 100
            waiting._save()
            blocked = RoundTripBot(quiet, book, Strategy(quiet.to_strategy_params()), NullFeed())
            blocked._restore_bookkeeping()
            blocked.step()
            self.assertEqual(blocked.phase, "FLAT")
            self.assertEqual(book.list_open_orders(quiet.market), [])

    def test_partial_buy_accumulates_then_take_profit_lot_only(self) -> None:
        self.bot.step()
        self.assertEqual(self.bot.phase, "BUY_WAIT")
        order = self.exchange.get_order(self.bot.order_uuid or "")
        assert order is not None and order.price is not None
        partial = floor_to_precision(order.volume / Decimal("2"), self.cfg.volume_precision)
        self.assertGreater(partial, Decimal("0"))
        self.exchange.fill_buy_partial(order.uuid, partial)
        self.bot.order_placed_at = 0
        self.bot.step()
        self.assertIsNotNone(self.bot.position)
        assert self.bot.position is not None
        self.assertEqual(self.bot.position.volume, partial)
        self.assertEqual(self.bot.position.stop_price, Decimal("0"))
        if self.bot.phase == "BUY_WAIT":
            self.market.trade_price = self.exchange.get_order(self.bot.order_uuid or "").price or order.price
            self.market.best_bid = self.market.trade_price
            self.market.best_ask = self.market.trade_price + Decimal("0.01")
            self.bot.step()
        if self.bot.phase == "FLAT":
            self.bot.step()
            if self.bot.phase == "BUY_WAIT":
                follow = self.exchange.get_order(self.bot.order_uuid or "")
                assert follow is not None and follow.price is not None
                self.market.trade_price = follow.price
                self.market.best_bid = follow.price
                self.market.best_ask = follow.price + Decimal("0.01")
                self.bot.step()
        self.assertIn(self.bot.phase, {"HOLDING", "SELL_WAIT"})
        assert self.bot.position is not None
        lot = self.bot.position.volume
        if self.bot.phase == "HOLDING":
            self.bot.step()
        self.assertEqual(self.bot.phase, "SELL_WAIT")
        sell = self.exchange.get_order(self.bot.order_uuid or "")
        assert sell is not None
        self.assertEqual(sell.volume, lot)

    def test_take_profit_volume_ignores_foreign_base(self) -> None:
        self._enter_sell_wait()
        assert self.bot.position is not None
        lot = self.bot.position.volume
        self.exchange._base_available += Decimal("0.05")
        self.bot.order_placed_at = 0
        open_sell = self.exchange.get_order(self.bot.order_uuid or "")
        assert open_sell is not None
        self.exchange.cancel(open_sell.uuid)
        self.bot.order_uuid = None
        self.bot.phase = "HOLDING"
        self.bot.step()
        self.assertEqual(self.bot.phase, "SELL_WAIT")
        sell = self.exchange.get_order(self.bot.order_uuid or "")
        assert sell is not None
        self.assertEqual(sell.volume, lot)
        self.assertEqual(self.exchange.base_available(), Decimal("0.05"))

    def test_stale_backfill_keeps_fixed_prices(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cfg = make_config(
                backfill_enabled=True,
                backfill_db=str(Path(folder) / "ohlcv.sqlite"),
                atr_period=2,
                range_lookback=2,
            )

            class OldBook:
                def get_ohlcv_page(self, market: str, unit: int, count: int = 200, to: str | None = None) -> list[OhlcvBar]:
                    del market, unit, count
                    if to:
                        return []
                    stamp = int(time.time() * 1000) - 3_600_000
                    return [
                        OhlcvBar(stamp, "old", Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1"))
                    ]

            bot = RoundTripBot(cfg, DryRunExchange(cfg, OldBook()), Strategy(cfg.to_strategy_params()), NullFeed())
            bot._refresh_dynamic_levels()
            self.assertIsNone(bot.strategy.levels)


if __name__ == "__main__":
    unittest.main()
