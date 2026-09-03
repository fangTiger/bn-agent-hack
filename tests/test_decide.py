from datetime import datetime, timezone

import pytest

import src.decide as decide_module
from src.calibrate import GapProfile
from src.market_clock import MarketClock


def utc(value: str) -> datetime:
    """把测试中的 ISO 文本转换为 UTC 时间。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


@pytest.fixture(autouse=True)
def isolated_decision_log(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(decide_module, "LOG_DIR", tmp_path)


def profile(p90: float = 0.05, n_samples: int = 30) -> GapProfile:
    return GapProfile(
        symbol="TESTUSDT",
        sigma_gap=0.04,
        p90=p90,
        p95=p90 * 1.1,
        stale_ratio=4.0,
        n_samples=n_samples,
    )


def inputs(
    *,
    quantity: float = 1.0,
    mid: float = 100.0,
    p90: float = 0.05,
    n_samples: int = 30,
    price_time: datetime | None = None,
) -> tuple[dict, dict, dict, dict]:
    now = utc("2026-09-04T19:45Z")
    positions = {"TESTUSDT": {"quantity": quantity}}
    prices = {
        "TESTUSDT": {
            "mid": mid,
            "weighted_avg_5m": mid,
            "timestamp": price_time or now,
        }
    }
    profiles = {"TESTUSDT": profile(p90=p90, n_samples=n_samples)}
    filters = {
        "TESTUSDT": {
            "stepSize": "0.1",
            "tickSize": "0.01",
            "minNotional": "5",
        }
    }
    return positions, prices, profiles, filters


def guard(
    positions: dict,
    prices: dict,
    profiles: dict,
    filters: dict,
    *,
    equity: float = 1000.0,
) -> list[decide_module.Decision]:
    return decide_module.pre_close_guard(
        utc("2026-09-04T19:45Z"),
        positions,
        prices,
        profiles,
        filters,
        equity,
        MarketClock(),
    )


def test_trims_to_equal_risk_budget() -> None:
    positions, prices, profiles, filters = inputs(quantity=10, p90=0.10)

    decision = guard(positions, prices, profiles, filters)[0]

    assert decision.action == "TRIM"
    assert decision.quantity == 8.5
    assert decision.stop_price is None
    assert decision.limit_price is None
    assert decision.risk_before == pytest.approx(100.0)
    assert decision.risk_after == pytest.approx(15.0)
    assert "Stress loss" in decision.reason


def test_places_stop_with_rounded_prices() -> None:
    positions, prices, profiles, filters = inputs(quantity=1.04, p90=0.05)

    decision = guard(positions, prices, profiles, filters)[0]

    assert decision.action == "PLACE_STOP"
    assert decision.quantity == 1.0
    assert decision.stop_price == 95.0
    assert decision.limit_price == 93.0
    assert decision.risk_before == pytest.approx(5.2)
    assert decision.risk_after == pytest.approx(5.2)
    assert "protective stop" in decision.reason


def test_stop_min_notional_uses_limit_leg() -> None:
    positions, prices, profiles, filters = inputs(quantity=0.05, p90=0.05)
    filters["TESTUSDT"]["stepSize"] = "0.01"

    decision = guard(positions, prices, profiles, filters)[0]

    assert decision.action == "ABSTAIN"
    assert "limit leg" in decision.reason
    assert decision.limit_price == 93.0


def test_abstains_when_limit_touches_percent_price_boundary() -> None:
    positions, prices, profiles, filters = inputs(quantity=1, p90=0.09)

    decision = guard(positions, prices, profiles, filters)[0]

    assert decision.action == "ABSTAIN"
    assert "price floor" in decision.reason


@pytest.mark.parametrize(
    ("field", "expected_reason"),
    [
        ("stale", "stale"),
        ("samples", "Insufficient calibration samples"),
        ("missing", "Market data missing"),
    ],
)
def test_data_errors_abstain(field: str, expected_reason: str) -> None:
    positions, prices, profiles, filters = inputs()
    if field == "stale":
        prices["TESTUSDT"]["timestamp"] = utc("2026-09-04T19:39Z")
    elif field == "samples":
        profiles["TESTUSDT"] = profile(n_samples=19)
    else:
        prices.clear()

    decision = guard(positions, prices, profiles, filters)[0]

    assert decision.action == "ABSTAIN"
    assert expected_reason in decision.reason


def test_same_session_and_symbol_is_idempotent() -> None:
    positions, prices, profiles, filters = inputs()

    first = guard(positions, prices, profiles, filters)[0]
    second = guard(positions, prices, profiles, filters)[0]

    assert first.action == "PLACE_STOP"
    assert second.action == "ABSTAIN"
    assert "duplicate" in second.reason
