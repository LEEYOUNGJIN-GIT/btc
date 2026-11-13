import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from backfill import OhlcvBar
from backtest_ladder import simulate_ladder
from config import validate_live_safety
from exchange_api import DryRunExchange, ScriptedMarket
from ladder import BUYING, EMPTY, HOLDING, SELLING, LadderBot, LadderConfigError, anchor_from_daily, level_price, validate_ladder
from test_bot import make_config
from test_ops import RecordingNotifier

D = Decimal


class DailyMarket(ScriptedMarket):
    def __init__(self, closes: list[str]) -> None:
        super().__init__()
        self.closes = closes

    def get_ohlcv_page(self, market: str, unit: int, count: int = 200, to: str | None = None) -> list[OhlcvBar]:
        bars = [OhlcvBar(i * 86_400_000, str(i), D(c), D(c), D(c), D(c), D("1")) for i, c in enumerate(self.closes)]
        return list(reversed(bars[-count:]))  # 업비트처럼 최신 봉이 먼저


class LadderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = self.make_cfg()
        # 완성 봉 최고 종가 100, 마지막 봉(진행 중) 130 은 무시돼야 합니다.
        self.market = DailyMarket(["90", "100", "95", "130"])
        self.set_price("100")
        self.exchange = DryRunExchange(self.cfg, self.market)
        self.notifier = RecordingNotifier()
        self.bot = LadderBot(self.cfg, self.exchange, self.notifier)

    def make_cfg(self, **overrides: object):
        values: dict[str, object] = dict(
            strategy_mode="ladder",
            buy_fee_rate=D("0.0025"),
            sell_fee_rate=D("0.0025"),
            ladder_step_rate=D("0.03"),
            ladder_slots=5,
            ladder_lookback_days=3,
            ladder_budget_quote=D("500"),
            ladder_max_open_buys=2,
            ladder_anchor_refresh_sec=3600,
            ladder_anchor_mode="high",
            ladder_state_file=str(self.dir / "ladder_state.json"),
            kill_switch_file=str(self.dir / "STOP"),
            flatten_file=str(self.dir / "FLATTEN"),
            heartbeat_file=str(self.dir / "hb"),
        )
        values.update(overrides)
        return make_config(**values)

    def set_price(self, price: str, spread: str = "0.02") -> None:
        p = D(price)
        self.market.trade_price = p
        self.market.best_bid = p - D(spread) / 2
        self.market.best_ask = p + D(spread) / 2

    def by_status(self, status: str) -> list[int]:
        return [s.index for s in self.bot.slots if s.status == status]


class AnchorTests(LadderTestCase):
    def test_anchor_ignores_in_progress_bar(self) -> None:
        self.bot.boot()
        self.assertEqual(self.bot.anchor, D("100"))

    def test_anchor_respects_lookback(self) -> None:
        bars = DailyMarket(["200", "90", "100", "95", "130"]).get_ohlcv_page("m", 1440, 200)
        self.assertEqual(anchor_from_daily(bars, 3), D("100"))
        self.assertEqual(anchor_from_daily(bars, 10), D("200"))

    def test_levels_step_down(self) -> None:
        self.assertEqual(level_price(D("100"), D("0.03"), 1), D("97"))
        self.assertEqual(level_price(D("100"), D("0.03"), 2), D("94.09"))


class LadderFlowTests(LadderTestCase):
    def test_places_only_nearest_buys(self) -> None:
        self.bot.step()
        self.assertEqual(self.by_status(BUYING), [1, 2])
        self.assertEqual(self.bot.slots[0].order_price, "97")
        self.assertEqual(self.bot.slots[1].order_price, "94.09")

    def test_buy_fill_then_sell_at_step_above_entry(self) -> None:
        self.bot.step()
        self.set_price("96.9")
        self.bot.step()
        slot = self.bot.slots[0]
        self.assertEqual(slot.status, SELLING)
        self.assertEqual(slot.entry, "97")
        self.assertEqual(slot.order_price, "99.91")  # 97 * 1.03
        self.assertEqual(self.by_status(BUYING), [2, 3])

    def test_round_trip_realizes_profit_and_reopens_slot(self) -> None:
        self.bot.step()
        self.set_price("96.9")
        self.bot.step()
        self.set_price("100")
        self.bot.step()
        self.assertEqual(self.bot.cycles, 1)
        # 100 USDT 구간, 3% 차익에서 왕복 수수료 약 0.5% 를 뺀 값
        self.assertGreater(self.bot.realized, D("2.3"))
        self.assertLess(self.bot.realized, D("2.6"))
        self.assertEqual(self.bot.slots[0].status, BUYING)
        self.assertTrue(any("매도 체결" in m for m in self.notifier.messages))

    def test_price_already_below_level_buys_at_ask_not_level(self) -> None:
        self.set_price("90")
        self.bot.step()
        self.assertEqual(self.bot.slots[0].order_price, "90.01")
        self.set_price("90")
        self.bot.step()
        self.assertEqual(self.bot.slots[0].entry, "90.01")

    def test_crash_never_spends_more_than_budget(self) -> None:
        start = self.exchange.quote_available()
        for price in ("96", "93", "90", "87", "84", "80", "60", "40"):
            self.set_price(price)
            self.bot.step()
            self.bot.step()
        self.assertEqual(len(self.by_status(SELLING)), 5)
        spent = start - self.exchange.quote_available()
        self.assertLessEqual(spent, D("500"))
        self.assertGreater(spent, D("495"))

    def test_anchor_rise_reprices_open_buys(self) -> None:
        self.bot.step()
        self.market.closes = ["90", "100", "95", "110", "130"]
        self.set_price("110")
        self.bot.anchor_ts = 0
        self.bot.step()
        self.assertEqual(self.bot.anchor, D("110"))
        self.assertEqual(self.bot.slots[0].order_price, "106.7")
        self.assertEqual(len(self.exchange.list_open_orders(self.cfg.market)), 2)


class LadderSafetyTests(LadderTestCase):
    def test_stop_file_cancels_buys_keeps_sells(self) -> None:
        self.bot.step()
        self.set_price("96.9")
        self.bot.step()
        Path(self.cfg.kill_switch_file).write_text("")
        self.bot.step()
        self.assertEqual(self.by_status(BUYING), [])
        self.assertEqual(self.by_status(SELLING), [1])
        sides = {o.side for o in self.exchange.list_open_orders(self.cfg.market)}
        self.assertEqual(sides, {"ask"})

    def test_flatten_sells_only_bot_volume(self) -> None:
        self.exchange._base_available += D("1")  # 봇과 무관한 장기 보유분
        self.bot.step()
        self.set_price("96.9")
        self.bot.step()
        self.assertGreater(self.bot.held_volume(), 0)
        Path(self.cfg.flatten_file).write_text("")
        self.set_price("90")
        self.bot.step()
        self.assertEqual(self.bot.held_volume(), 0)
        self.assertEqual(self.exchange.base_total(), D("1"))
        self.assertLess(self.bot.realized, 0)
        self.assertEqual(self.exchange.list_open_orders(self.cfg.market), [])
        self.bot.step()
        self.assertEqual(self.by_status(BUYING), [])
        os.remove(self.cfg.flatten_file)
        self.bot.step()
        self.assertEqual(self.by_status(BUYING), [1, 2])

    def test_manual_sale_of_bot_coin_does_not_crash(self) -> None:
        self.bot.step()
        self.set_price("96.9")
        self.bot.step()
        slot = self.bot.slots[0]
        self.exchange.cancel(slot.order_uuid)
        self.exchange._base_available = D("0")  # 사용자가 밖에서 팔아버린 상황
        with self.assertLogs("scalper", level="WARNING"):
            self.bot.step()
            self.bot.step()
        self.assertEqual(slot.status, HOLDING)

    def test_step_must_cover_fees(self) -> None:
        cfg = self.make_cfg(ladder_step_rate=D("0.008"))
        with self.assertRaises(LadderConfigError):
            validate_ladder(cfg, D("0.0025"), D("0.0025"))
        validate_ladder(cfg, D("0.0005"), D("0.0005"))

    def test_slot_size_must_clear_min_order(self) -> None:
        cfg = self.make_cfg(ladder_budget_quote=D("4"), ladder_slots=5)
        with self.assertRaises(LadderConfigError):
            validate_ladder(cfg, D("0.0025"), D("0.0025"))

    def test_live_requires_budget(self) -> None:
        cfg = self.make_cfg(dry_run=False, access_key="a", secret_key="b", live_trading_confirm=True, ladder_budget_quote=D("0"))
        with self.assertRaises(ValueError):
            validate_live_safety(cfg)
        cfg.ladder_budget_quote = D("300")
        validate_live_safety(cfg)


class LadderStateTests(LadderTestCase):
    def live_cfg(self):
        return self.make_cfg(dry_run=False, access_key="a", secret_key="b", live_trading_confirm=True)

    def test_live_restart_restores_slots_and_orders(self) -> None:
        cfg = self.live_cfg()
        exchange = DryRunExchange(cfg, self.market)
        bot = LadderBot(cfg, exchange)
        bot.step()
        self.set_price("96.9")
        bot.step()
        self.set_price("100")
        bot.step()
        self.set_price("96.9")
        bot.step()
        saved = json.loads(Path(cfg.ladder_state_file).read_text())
        self.assertEqual(saved["cycles"], 1)
        again = LadderBot(cfg, exchange)
        again.boot()
        self.assertEqual(again.cycles, 1)
        self.assertEqual(again.realized, bot.realized)
        self.assertEqual([s.status for s in again.slots], [s.status for s in bot.slots])
        before = len(exchange.list_open_orders(cfg.market))
        again.step()
        self.assertEqual(len(exchange.list_open_orders(cfg.market)), before)

    def test_live_refuses_when_orders_exist_without_state(self) -> None:
        cfg = self.live_cfg()
        exchange = DryRunExchange(cfg, self.market)
        exchange.buy_limit(cfg.market, D("50"), D("1"), False)
        with self.assertRaises(LadderConfigError):
            LadderBot(cfg, exchange).boot()

    def test_state_from_other_mode_is_rejected(self) -> None:
        self.bot.step()
        cfg = self.live_cfg()
        with self.assertRaises(LadderConfigError):
            LadderBot(cfg, DryRunExchange(cfg, self.market)).boot()


class PriceAnchorTests(LadderTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = self.make_cfg(ladder_anchor_mode="price")
        self.set_price("80")
        self.exchange = DryRunExchange(self.cfg, self.market)
        self.bot = LadderBot(self.cfg, self.exchange)

    def test_anchor_starts_at_current_price_and_buys_nothing_immediately(self) -> None:
        self.bot.step()
        self.assertEqual(self.bot.anchor, D("80"))
        self.assertEqual(self.bot.slots[0].order_price, "77.6")
        self.assertEqual(self.bot.held_volume(), 0)

    def test_anchor_follows_price_up_only_while_flat(self) -> None:
        self.bot.step()
        self.set_price("80.2")  # 0.5% 미만: 그대로
        self.bot.step()
        self.assertEqual(self.bot.anchor, D("80"))
        self.set_price("84")
        self.bot.step()
        self.assertEqual(self.bot.anchor, D("84"))
        self.assertEqual(self.bot.slots[0].order_price, "81.48")
        self.set_price("81.4")
        self.bot.step()  # 구간 1 체결 -> 보유 중
        self.set_price("83.5")
        self.bot.step()
        self.set_price("90")  # 구간 1 은 83.93 에서 매도되고, 그 틱에 보유가 없어져 앵커가 올라감
        self.bot.step()
        self.assertEqual(self.bot.cycles, 1)
        self.assertEqual(self.bot.anchor, D("90"))

    def test_anchor_never_moves_while_holding_or_down(self) -> None:
        self.bot.step()
        self.set_price("77.5")
        self.bot.step()
        self.assertEqual(self.by_status(SELLING), [1])
        for price in ("79.5", "70", "60"):
            self.set_price(price)
            self.bot.step()
            self.assertEqual(self.bot.anchor, D("80"))

    def test_live_restart_keeps_anchor(self) -> None:
        cfg = self.make_cfg(ladder_anchor_mode="price", dry_run=False, access_key="a", secret_key="b", live_trading_confirm=True)
        exchange = DryRunExchange(cfg, self.market)
        bot = LadderBot(cfg, exchange)
        bot.step()
        self.set_price("77.5")
        bot.step()
        self.set_price("70")
        again = LadderBot(cfg, exchange)
        again.boot()
        self.assertEqual(again.anchor, D("80"))


class LadderBacktestTests(unittest.TestCase):
    @staticmethod
    def bars(closes: list[float]) -> list[tuple[float, float, float, float]]:
        out = []
        prev = closes[0]
        for c in closes:
            out.append((prev, max(prev, c), min(prev, c), c))
            prev = c
        return out

    def test_oscillation_earns_and_ends_flat(self) -> None:
        closes = [100.0] * 5 + [100.0, 96.0, 100.5] * 20
        r = simulate_ladder(self.bars(closes), 5, len(closes), step=0.03, slots=5, lookback=5, fee=0.0025, slip=0.0, anchor_mode="high")
        self.assertGreater(r.sells, 5)
        self.assertGreater(r.quote_return, 0)
        self.assertEqual(r.slots_held, 0)

    def test_crash_fills_every_slot_and_loses(self) -> None:
        closes = [100.0] * 5 + [100.0 * 0.97 ** i for i in range(1, 15)]
        r = simulate_ladder(self.bars(closes), 5, len(closes), step=0.03, slots=5, lookback=5, fee=0.0025, slip=0.0, anchor_mode="high")
        self.assertEqual(r.slots_held, 5)
        self.assertEqual(r.sells, 0)
        self.assertLess(r.quote_return, -0.15)

    def test_anchor_uses_only_past_bars(self) -> None:
        # 마지막 날 급등 종가가 그날의 매수 구간을 끌어올리면 안 됩니다.
        closes = [100.0] * 6 + [99.0, 150.0]
        r = simulate_ladder(self.bars(closes), 5, len(closes), step=0.03, slots=5, lookback=5, fee=0.0, slip=0.0, anchor_mode="high")
        self.assertEqual(r.slots_held, 0)


class PriceAnchorBacktestTests(unittest.TestCase):
    def test_price_anchor_starts_at_start_price_and_ratchets_when_flat(self) -> None:
        bars = LadderBacktestTests.bars
        # 과거 고점 200 은 무시하고 시작가 100 기준. 97 아래로 안 내려가면 매수 없음.
        closes = [200.0] * 5 + [100.0, 98.0, 99.0, 98.0]
        r = simulate_ladder(bars(closes), 5, len(closes), step=0.03, slots=5, lookback=5, fee=0.0, slip=0.0, anchor_mode="price")
        self.assertEqual((r.slots_held, r.sells), (0, 0))
        # 120 까지 오른 뒤 3% 내리면 새 앵커(120) 기준으로 매수
        closes = [200.0] * 5 + [100.0, 120.0, 120.0, 116.0]
        r = simulate_ladder(bars(closes), 5, len(closes), step=0.03, slots=5, lookback=5, fee=0.0, slip=0.0, anchor_mode="price")
        self.assertEqual(r.slots_held, 1)
        r = simulate_ladder(bars(closes), 5, len(closes), step=0.03, slots=5, lookback=5, fee=0.0, slip=0.0, anchor_mode="fixed")
        self.assertEqual(r.slots_held, 0)


if __name__ == "__main__":
    unittest.main()
