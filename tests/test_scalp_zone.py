import unittest
from decimal import Decimal

from backfill import OhlcvBar
from scalp_zone import compute_scalping_zone, suggest_limit_buy


def _series(
    count: int,
    *,
    start_ms: int,
    step_ms: int,
    base: Decimal,
    swing: Decimal,
) -> list[OhlcvBar]:
    bars: list[OhlcvBar] = []
    for index in range(count):
        # 초반은 저가, 후반은 고가 쪽으로 살짝 올립니다.
        level = base + swing * Decimal(index) / Decimal(max(count - 1, 1))
        bars.append(
            OhlcvBar(
                timestamp_ms=start_ms + index * step_ms,
                open_time=f"t{index}",
                open=level,
                high=level + swing,
                low=level - swing,
                close=level,
                volume=Decimal("1"),
            )
        )
    return bars


class ScalpZoneTests(unittest.TestCase):
    def test_long_bias_near_daily_support(self) -> None:
        daily = _series(150, start_ms=0, step_ms=86_400_000, base=Decimal("100"), swing=Decimal("2"))
        # 종가를 박스 하단에 가깝게: 마지막 close 를 낮춤
        last = daily[-1]
        daily[-1] = OhlcvBar(
            last.timestamp_ms,
            last.open_time,
            Decimal("98"),
            Decimal("99"),
            Decimal("97"),
            Decimal("98"),
            Decimal("1"),
        )
        minute = _series(150, start_ms=last.timestamp_ms, step_ms=60_000, base=Decimal("98"), swing=Decimal("0.2"))
        zone = compute_scalping_zone(
            minute,
            daily,
            market="USDT-BTC",
            entry_zone=Decimal("0.40"),
        )
        self.assertEqual(zone.market, "USDT-BTC")
        self.assertIn(zone.bias, {"long_bias", "neutral", "avoid"})
        self.assertLess(zone.zone_low, zone.zone_high)
        self.assertGreater(zone.target_rate, 0)
        self.assertGreater(zone.target_sell, zone.reference_buy)

    def test_avoid_on_daily_breakdown(self) -> None:
        daily = _series(150, start_ms=0, step_ms=86_400_000, base=Decimal("100"), swing=Decimal("1"))
        # 지지보다 훨씬 아래 종가
        last = daily[-1]
        daily[-1] = OhlcvBar(
            last.timestamp_ms,
            last.open_time,
            Decimal("80"),
            Decimal("81"),
            Decimal("79"),
            Decimal("80"),
            Decimal("1"),
        )
        minute = _series(150, start_ms=last.timestamp_ms, step_ms=60_000, base=Decimal("80"), swing=Decimal("0.1"))
        zone = compute_scalping_zone(minute, daily, market="USDT-ETH")
        self.assertEqual(zone.bias, "avoid")
        self.assertFalse(zone.actionable)
        self.assertIsNone(suggest_limit_buy(zone, Decimal("80"), Decimal("80.1")))


if __name__ == "__main__":
    unittest.main()
