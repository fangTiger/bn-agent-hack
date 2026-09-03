import math
import statistics
from datetime import datetime, timezone

import pytest

import src.calibrate as calibrate_module
from src.market_clock import MarketClock


def milliseconds(value: str) -> int:
    """把测试时间转换成毫秒时间戳。"""
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(timestamp.timestamp() * 1000)


def bar(value: str, open_price: float, close_price: float) -> list:
    """构造只包含标定所需字段的币安 K 线。"""
    return [milliseconds(value), str(open_price), "0", "0", str(close_price)]


def linear_percentile(values: list[float], probability: float) -> float:
    """计算测试期望值所用的线性插值分位数。"""
    ordered = sorted(values)
    rank = (len(ordered) - 1) * probability
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def test_calibrate_uses_every_session_open_and_dst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = [
        bar("2026-09-01T13:30Z", 100, 110),
        bar("2026-09-01T23:00Z", 100, 101),
        bar("2026-09-02T13:30Z", 100, 90),
        bar("2026-09-02T23:00Z", 100, 102),
        bar("2026-11-02T14:30Z", 100, 105),
        bar("2026-11-02T23:00Z", 100, 99),
    ]
    monkeypatch.setattr(calibrate_module, "fetch_klines", lambda symbol, interval="30m": bars)

    profile = calibrate_module.calibrate("TESTUSDT", MarketClock())

    opening_returns = [math.log(1.10), math.log(0.90), math.log(1.05)]
    closed_returns = [math.log(1.01), math.log(1.02), math.log(0.99)]
    absolute_opening = [abs(value) for value in opening_returns]
    assert profile.symbol == "TESTUSDT"
    assert profile.n_samples == 3
    assert profile.sigma_gap == pytest.approx(statistics.stdev(opening_returns))
    assert profile.p90 == pytest.approx(linear_percentile(absolute_opening, 0.90))
    assert profile.p95 == pytest.approx(linear_percentile(absolute_opening, 0.95))
    assert profile.stale_ratio == pytest.approx(
        statistics.mean(absolute_opening)
        / statistics.mean(abs(value) for value in closed_returns)
    )


def test_calibrate_requires_enough_values_for_statistics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        calibrate_module,
        "fetch_klines",
        lambda symbol, interval="30m": [bar("2026-09-01T13:30Z", 100, 101)],
    )

    with pytest.raises(ValueError, match="samples"):
        calibrate_module.calibrate("TESTUSDT", MarketClock())


def test_fetch_klines_starts_at_zero_and_uses_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    batches = [
        [[index * 1_800_000, "1", "1", "1", "1"] for index in range(1000)],
        [[1_800_000_000, "1", "1", "1", "1"]],
    ]
    requested_urls: list[str] = []

    class Response:
        def __init__(self, payload: list) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def read(self) -> bytes:
            import json

            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(url: str, timeout: int):
        requested_urls.append(url)
        return Response(batches.pop(0))

    monkeypatch.setattr(calibrate_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(calibrate_module.urllib.request, "urlopen", fake_urlopen)

    first = calibrate_module.fetch_klines("TESTUSDT")
    second = calibrate_module.fetch_klines("TESTUSDT")

    assert "startTime=0" in requested_urls[0]
    assert "startTime=1798200001" in requested_urls[1]
    assert len(first) == 1001
    assert second == first
    assert len(requested_urls) == 2
