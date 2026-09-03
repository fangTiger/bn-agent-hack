from datetime import datetime, timezone

import pytest

from src.market_clock import MarketClock


def utc(value: str) -> datetime:
    """把测试中的 ISO 文本转换为 UTC 时间。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@pytest.fixture
def clock() -> MarketClock:
    return MarketClock()


def test_labor_day_2026(clock: MarketClock) -> None:
    # 09-04 周五收盘后，下一次开盘不是周一而是周二。
    start = utc("2026-09-04T20:00Z")
    end = utc("2026-09-08T13:30Z")
    assert clock.next_open(start) == end
    assert clock.openings_between(start, end) == 1
    assert "Labor Day" in (clock.skipped_reason(start, end) or "")


def test_weekend_is_one_opening_not_three(clock: MarketClock) -> None:
    # 核心论点：周末只有 1 次开盘，不是 3 天。
    assert clock.openings_between(
        utc("2026-09-11T20:00Z"), utc("2026-09-14T13:30Z")
    ) == 1


def test_dst_transition(clock: MarketClock) -> None:
    # 夏令时结束后，开盘时刻由纽约本地时间换算为 14:30 UTC。
    assert clock.next_open(utc("2026-11-02T00:00Z")) == utc(
        "2026-11-02T14:30Z"
    )


def test_half_day(clock: MarketClock) -> None:
    assert clock.next_close(utc("2026-11-27T14:00Z")) == utc(
        "2026-11-27T18:00Z"
    )


def test_juneteenth_and_july3(clock: MarketClock) -> None:
    # 这两天是因果证据的来源，必须识别为休市。
    assert clock.state(utc("2026-06-19T15:00Z")) == "CLOSED"
    assert clock.state(utc("2026-07-03T15:00Z")) == "CLOSED"


def test_state_boundaries(clock: MarketClock) -> None:
    assert clock.state(utc("2026-09-02T13:29Z")) == "CLOSED"
    assert clock.state(utc("2026-09-02T13:30Z")) == "OPEN"
    assert clock.state(utc("2026-09-02T19:59Z")) == "OPEN"
    assert clock.state(utc("2026-09-02T20:00Z")) == "CLOSED"


def test_rejects_naive_datetime(clock: MarketClock) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.state(datetime(2026, 9, 2, 13, 30))
