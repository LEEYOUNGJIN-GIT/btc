import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import preflight
from config import validate_live_safety
from exchange_api import DryRunExchange, NullFeed, ScriptedMarket, UpbitAPIError
from hints import explain_upbit_error
from main import RoundTripBot
from notifier import HttpNotifier, Notifier, build_notifier
from strategy import Strategy
from test_bot import make_config


class RecordingNotifier(Notifier):
    enabled = True

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.pings = 0

    def notify(self, text: str, *, key: str | None = None, cooldown: float = 0.0) -> None:
        self.messages.append(text)

    def ping(self) -> None:
        self.pings += 1


class NotifierTests(unittest.TestCase):
    def test_noop_without_env(self) -> None:
        cfg = make_config()
        notifier = build_notifier(cfg)
        self.assertFalse(notifier.enabled)
        notifier.notify("x")
        notifier.ping()

    def test_cooldown_throttles_same_key(self) -> None:
        calls: list[dict] = []
        notifier = HttpNotifier("tok", "chat", post=lambda url, **kw: calls.append(kw), background=False)
        notifier.notify("a", key="k", cooldown=60)
        notifier.notify("b", key="k", cooldown=60)
        notifier.notify("c")
        self.assertEqual(len(calls), 2)
        self.assertIn("[btc-bot] a", calls[0]["json"]["text"])

    def test_send_failure_never_raises_and_hides_token(self) -> None:
        def boom(*a: object, **k: object) -> None:
            raise RuntimeError("https://api.telegram.org/botSECRETTOKEN/sendMessage failed")

        notifier = HttpNotifier("SECRETTOKEN", "chat", post=boom, background=False)
        with self.assertLogs("scalper", level="WARNING") as logs:
            notifier.notify("hello")
        self.assertNotIn("SECRETTOKEN", "\n".join(logs.output))

    def test_ping_is_throttled(self) -> None:
        hits: list[str] = []
        notifier = HttpNotifier("", "", "https://hc.example/ping", get=lambda url, **kw: hits.append(url), background=False)
        notifier.ping()
        notifier.ping()
        self.assertEqual(len(hits), 1)


class LiveSafetyTests(unittest.TestCase):
    def test_live_requires_loss_limit(self) -> None:
        cfg = make_config(dry_run=False, access_key="a", secret_key="b", live_trading_confirm=True)
        with self.assertRaises(ValueError):
            validate_live_safety(cfg)
        cfg.max_daily_loss_quote = Decimal("5")
        validate_live_safety(cfg)

    def test_explicit_opt_out_allowed(self) -> None:
        cfg = make_config(dry_run=False, access_key="a", secret_key="b", live_trading_confirm=True)
        cfg.allow_no_loss_limit = True
        validate_live_safety(cfg)

    def test_dry_run_needs_no_limit(self) -> None:
        validate_live_safety(make_config())

    def test_telegram_pair_must_match(self) -> None:
        cfg = make_config()
        cfg.telegram_bot_token = "x"
        with self.assertRaises(ValueError):
            validate_live_safety(cfg)


class HintTests(unittest.TestCase):
    def test_known_and_unknown(self) -> None:
        self.assertIn("허용 IP", explain_upbit_error("no_authorization_ip"))
        self.assertIn("weird", explain_upbit_error("weird", "msg"))


class BotOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stop = str(Path(self.tmp.name) / "STOP")
        self.flatten = str(Path(self.tmp.name) / "FLATTEN")
        self.hb = str(Path(self.tmp.name) / "hb")
        self.cfg = make_config(
            kill_switch_file=self.stop, flatten_file=self.flatten, heartbeat_file=self.hb
        )
        self.market = ScriptedMarket()
        self.market.trade_price = Decimal("99.20")
        self.market.best_bid = Decimal("99.20")
        self.market.best_ask = Decimal("99.22")
        self.market.box_low = Decimal("99")
        self.market.box_high = Decimal("101")
        self.exchange = DryRunExchange(self.cfg, self.market)
        self.notifier = RecordingNotifier()
        self.bot = RoundTripBot(
            self.cfg, self.exchange, Strategy(self.cfg.to_strategy_params()), NullFeed(), self.notifier
        )

    def _fill_buy(self) -> None:
        self.bot.step()
        order = self.exchange.get_order(self.bot.order_uuid or "")
        assert order is not None and order.price is not None
        self.market.trade_price = order.price
        self.market.best_bid = order.price
        self.market.best_ask = order.price + Decimal("0.01")
        self.bot.step()
        self.assertEqual(self.bot.phase, "SELL_WAIT")

    def test_stop_file_blocks_new_entries(self) -> None:
        Path(self.stop).write_text("")
        self.bot.step()
        self.assertEqual(self.bot.phase, "FLAT")
        self.assertEqual(self.exchange.list_open_orders(self.cfg.market), [])

    def test_stop_file_cancels_pending_buy(self) -> None:
        self.bot.step()
        self.assertEqual(self.bot.phase, "BUY_WAIT")
        Path(self.stop).write_text("")
        self.bot.step()
        self.assertEqual(self.bot.phase, "FLAT")
        self.assertEqual(self.exchange.list_open_orders(self.cfg.market), [])

    def test_stop_file_keeps_managing_open_position(self) -> None:
        self._fill_buy()
        Path(self.stop).write_text("")
        self.bot.step()
        self.assertEqual(self.bot.phase, "SELL_WAIT")
        assert self.bot.position is not None
        self.market.trade_price = self.bot.position.stop_price
        self.market.best_bid = self.bot.position.stop_price
        self.bot.step()
        self.assertEqual(self.bot.phase, "FLAT")

    def test_flatten_file_liquidates(self) -> None:
        self._fill_buy()
        Path(self.flatten).write_text("")
        self.bot.step()
        self.assertEqual(self.bot.phase, "FLAT")
        self.assertEqual(self.exchange.base_total(), Decimal("0"))
        self.assertEqual(self.exchange.list_open_orders(self.cfg.market), [])
        self.bot.step()
        self.assertEqual(self.bot.phase, "FLAT")
        self.assertEqual(self.exchange.list_open_orders(self.cfg.market), [])

    def test_removing_stop_file_resumes_entries(self) -> None:
        Path(self.stop).write_text("")
        self.bot.step()
        os.remove(self.stop)
        self.bot.step()
        self.assertEqual(self.bot.phase, "BUY_WAIT")

    def test_max_order_quote_caps_size(self) -> None:
        self.cfg.max_order_quote = Decimal("20")
        self.bot.step()
        order = self.exchange.get_order(self.bot.order_uuid or "")
        assert order is not None and order.price is not None
        self.assertLessEqual(order.price * order.volume, Decimal("20"))

    def test_uncapped_uses_full_balance(self) -> None:
        self.bot.step()
        order = self.exchange.get_order(self.bot.order_uuid or "")
        assert order is not None and order.price is not None
        self.assertGreater(order.price * order.volume, Decimal("900"))

    def test_heartbeat_and_notifications(self) -> None:
        self._fill_buy()
        self.assertTrue(Path(self.hb).exists())
        self.assertTrue(any("매수 체결" in m for m in self.notifier.messages))

    def test_halt_notifies_once(self) -> None:
        self.bot.phase = "HALT"
        self.bot.step()
        self.bot.step()
        self.assertEqual(sum("HALT" in m for m in self.notifier.messages), 1)

    def test_error_streak_alerts_and_resets(self) -> None:
        self.cfg.error_alert_threshold = 2
        self.cfg.max_runtime_sec = 0
        calls = {"n": 0}

        def flaky() -> None:
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("x")
            self.bot.request_stop()

        self.bot.step = flaky  # type: ignore[method-assign]
        self.bot._boot = lambda: None  # type: ignore[method-assign]
        self.bot.run()
        self.assertTrue(any("연속" in m for m in self.notifier.messages))
        self.assertEqual(self.bot._err_streak, 0)


class FakeOrder:
    uuid = "u1"
    state = "cancel"


class FakeRest:
    def __init__(self, accounts=None, chance=None, error=None) -> None:
        self.accounts, self.chance, self.error = accounts, chance, error
        self.placed: list[tuple] = []
        self.cancelled: list[str] = []

    def _request(self, method, path, params, group, auth):
        if self.error:
            raise self.error
        return self.accounts if path == "/v1/accounts" else self.chance

    def get_orderbook(self, market):
        from exchange_api import OrderBook

        return OrderBook(market, Decimal("100"), Decimal("100.01"), Decimal("1"), Decimal("1"))

    def buy_limit(self, market, price, volume, post_only):
        self.placed.append((price, volume))
        return FakeOrder()

    def cancel(self, uuid):
        self.cancelled.append(uuid)
        return FakeOrder()


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = make_config()

    def test_accounts_ip_error_is_explained(self) -> None:
        err = UpbitAPIError("x", status=401, name="no_authorization_ip")
        ok, msg = preflight.check_accounts(FakeRest(error=err), self.cfg)
        self.assertFalse(ok)
        self.assertIn("허용 IP", msg)

    def test_accounts_success_reports_usdt(self) -> None:
        rest = FakeRest(accounts=[{"currency": "USDT", "balance": "12.5"}])
        ok, msg = preflight.check_accounts(rest, self.cfg)
        self.assertTrue(ok)
        self.assertIn("12.5", msg)

    def test_chance_flags_higher_min_total(self) -> None:
        rest = FakeRest(chance={"bid_fee": "0.0005", "ask_fee": "0.0005", "market": {"bid": {"min_total": "1", "currency": "USDT"}}})
        ok, msg = preflight.check_chance(rest, self.cfg)
        self.assertTrue(ok)
        self.assertIn("MIN_ORDER_QUOTE", msg)

    def test_order_test_places_far_below_market_and_cancels(self) -> None:
        rest = FakeRest()
        ok, _ = preflight.order_test(rest, self.cfg)
        self.assertTrue(ok)
        price, volume = rest.placed[0]
        self.assertLessEqual(price, Decimal("50"))
        self.assertGreaterEqual(price * volume, self.cfg.min_order_quote)
        self.assertEqual(rest.cancelled, ["u1"])

    def test_discover_public_ip_falls_back(self) -> None:
        class R:
            def __init__(self, code, text):
                self.status_code, self.text = code, text

        responses = iter([R(500, ""), R(200, "203.0.113.7\n")])

        def get(url, timeout):
            return next(responses)

        self.assertEqual(preflight.discover_public_ip(get), "203.0.113.7")

    def test_order_test_requires_yes(self) -> None:
        with mock.patch.object(preflight, "load_config", return_value=make_config(access_key="a", secret_key="b")), \
             mock.patch.object(preflight, "build_rest", return_value=FakeRest(accounts=[], chance={})):
            code = preflight.run(["--order-test"], get=lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()


class StopLossOptionTests(unittest.TestCase):
    def test_disabled_stop_loss_holds_through_drop(self) -> None:
        cfg = make_config(stop_loss_enabled=False)
        market = ScriptedMarket()
        market.trade_price = market.best_bid = Decimal("99.20")
        market.best_ask = Decimal("99.22")
        market.box_low, market.box_high = Decimal("99"), Decimal("101")
        ex = DryRunExchange(cfg, market)
        bot = RoundTripBot(cfg, ex, Strategy(cfg.to_strategy_params()), NullFeed())
        bot.step()
        order = ex.get_order(bot.order_uuid or "")
        market.trade_price = market.best_bid = order.price
        market.best_ask = order.price + Decimal("0.01")
        bot.step()
        self.assertEqual(bot.phase, "SELL_WAIT")
        market.trade_price = market.best_bid = bot.position.stop_price - Decimal("5")
        bot.step()
        self.assertEqual(bot.phase, "SELL_WAIT")
        self.assertGreater(ex.base_total(), 0)


class AnalysisFixTests(unittest.TestCase):
    def _bar(self, minute: int, price: str):
        from backfill import OhlcvBar

        p = Decimal(price)
        return OhlcvBar(minute * 60_000, "", p, p, p, p, Decimal("1"))

    def test_fill_gaps_restores_time_axis(self) -> None:
        from analyzer import fill_gaps

        bars = [self._bar(0, "100"), self._bar(5, "101")]
        filled = fill_gaps(bars, 60_000)
        self.assertEqual(len(filled), 6)
        self.assertEqual([b.close for b in filled[1:5]], [Decimal("100")] * 4)
        self.assertEqual(filled[-1].close, Decimal("101"))

    def test_gap_fill_lowers_sparse_range(self) -> None:
        from analyzer import analyze

        bars = []
        for i in range(30):
            from backfill import OhlcvBar

            p = Decimal("100") + (Decimal("0.5") if i % 2 else Decimal("0"))
            bars.append(OhlcvBar(i * 5 * 60_000, "", p, p + Decimal("0.3"), p - Decimal("0.3"), p, Decimal("1")))
        kw = dict(buy_fee_rate=Decimal("0.0005"), sell_fee_rate=Decimal("0.0005"), atr_period=14, range_lookback=120)
        raw = analyze(bars, **kw)
        filled = analyze(bars, period_ms=60_000, **kw)
        self.assertLess(filled.mean_range, raw.mean_range)
        self.assertGreater(filled.sample_size, raw.sample_size)

    def test_spread_gate_blocks_entry(self) -> None:
        cfg = make_config(max_spread_rate=Decimal("0.001"))
        market = ScriptedMarket()
        market.trade_price = market.best_bid = Decimal("99.20")
        market.best_ask = Decimal("99.60")
        market.box_low, market.box_high = Decimal("99"), Decimal("101")
        ex = DryRunExchange(cfg, market)
        bot = RoundTripBot(cfg, ex, Strategy(cfg.to_strategy_params()), NullFeed())
        bot.step()
        self.assertEqual(bot.phase, "FLAT")
        self.assertEqual(ex.list_open_orders(cfg.market), [])

    def test_backtest_simulate_basic(self) -> None:
        from backtest import simulate

        bars = []
        for i in range(3000):
            price = 100 + (i % 20) * 0.05
            p = Decimal(str(round(price, 2)))
            bars.append(self._bar(i, str(p)))
        result = simulate(bars, fee=0.0005, target=0.0025, grid=0.002, horizon=300)
        self.assertGreater(result.attempts, 0)

    def test_base_row_note_flags_foreign_unit(self) -> None:
        note = preflight.describe_base_rows(
            [{"currency": "BTC", "unit_currency": "KRW", "balance": "0.218", "locked": "0", "avg_buy_price": "47000000"}],
            make_config(),
        )
        self.assertIn("unit=KRW", note)
        self.assertIn("주의", note)
