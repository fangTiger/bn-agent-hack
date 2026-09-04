"""根据币安公开 30 分钟 K 线标定开盘跳空。"""

from __future__ import annotations

import json
import math
import statistics
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.market_clock import MarketClock


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
API_URL = "https://api.binance.com/api/v3/klines"


@dataclass
class GapProfile:
    symbol: str
    sigma_gap: float
    p90: float
    p95: float
    stale_ratio: float
    n_samples: int


def fetch_klines(symbol: str, interval: str = "30m") -> list:
    """从最早一根开始分页读取公开 K 线，并把结果缓存到本地。"""
    cache_path = DATA_DIR / f"{symbol}_{interval}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    bars: list = []
    start_time = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "symbol": symbol,
                "interval": interval,
                "limit": 1000,
                "startTime": start_time,
            }
        )
        with urllib.request.urlopen(f"{API_URL}?{query}", timeout=30) as response:
            batch = json.loads(response.read())
        if not isinstance(batch, list):
            raise RuntimeError(f"unexpected response from Binance public klines: {batch!r}")
        if not batch:
            break

        # 防御接口边界偶发的重复 K 线，缓存中每个开盘时间只保留一次。
        if bars and batch[0][0] == bars[-1][0]:
            batch = batch[1:]
        if not batch:
            break
        bars.extend(batch)

        if len(batch) < 1000:
            break
        next_start = int(batch[-1][0]) + 1
        if next_start <= start_time:
            raise RuntimeError("Binance kline pagination failed to advance")
        start_time = next_start

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(bars, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return bars


def to_utc(milliseconds: int) -> datetime:
    """把币安的毫秒时间戳转成带时区的 UTC 时间。"""
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)


def calibrate(symbol: str, clock: MarketClock) -> GapProfile:
    """计算标的开盘后首根收益分布和休市时段陈旧度倍数。"""
    bars = fetch_klines(symbol, interval="30m")
    opening_returns: list[float] = []
    closed_returns: list[float] = []

    for raw_bar in bars:
        if len(raw_bar) < 5:
            continue
        bar_time = datetime.fromtimestamp(
            int(raw_bar[0]) / 1000, tz=timezone.utc
        )
        try:
            open_price = float(raw_bar[1])
            close_price = float(raw_bar[4])
        except (TypeError, ValueError):
            continue
        if open_price <= 0 or close_price <= 0:
            continue

        log_return = math.log(close_price / open_price)
        if _is_opening_bar(bar_time, clock):
            opening_returns.append(log_return)
        elif clock.state(bar_time) == "CLOSED":
            closed_returns.append(log_return)

    if len(opening_returns) < 2:
        raise ValueError(f"{symbol}: too few opening samples to compute a standard deviation")
    if not closed_returns:
        raise ValueError(f"{symbol}: no off-hours samples available")

    closed_mean = statistics.mean(abs(value) for value in closed_returns)
    if closed_mean == 0:
        raise ValueError(f"{symbol}: off-hours volatility is zero; cannot compute a stale ratio")

    absolute_opening = [abs(value) for value in opening_returns]
    return GapProfile(
        symbol=symbol,
        sigma_gap=statistics.stdev(opening_returns),
        p90=_percentile(absolute_opening, 0.90),
        p95=_percentile(absolute_opening, 0.95),
        stale_ratio=statistics.mean(absolute_opening) / closed_mean,
        n_samples=len(opening_returns),
    )


def _is_opening_bar(bar_time: datetime, clock: MarketClock) -> bool:
    """由市场时钟判断 K 线是否恰好从常规开盘开始。"""
    if clock.state(bar_time) != "OPEN":
        return False
    return clock.state(bar_time - timedelta(microseconds=1)) == "CLOSED"


def _percentile(values: list[float], probability: float) -> float:
    """用相邻次序统计量线性插值计算分位数。"""
    if not values:
        raise ValueError("quantile requires at least one sample")
    if not 0 <= probability <= 1:
        raise ValueError("quantile probability must lie between 0 and 1")

    ordered = sorted(values)
    rank = (len(ordered) - 1) * probability
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight
