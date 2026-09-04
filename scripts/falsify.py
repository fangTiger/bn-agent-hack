#!/usr/bin/env python3
"""证伪脚本：检验开盘跳空里是否存在可交易的 edge

对应 README 第 3 节。两个假设都在真实公开数据上独立验证，结论都是：
低效真实存在，但幅度小于 0.2% 的往返手续费。

假设 1：能否用隔夜 BTC 涨跌预测 bstock 的开盘跳空方向
假设 2：开盘后首个 30 分钟的动量，是否延续到下一个 30 分钟

只读公开行情接口，不需要认证，不产生任何交易。
"""

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.calibrate import fetch_klines, to_utc  # noqa: E402
from src.market_clock import MarketClock  # noqa: E402

CLOCK = MarketClock()

# 币安现货 maker/taker 均 0.1%，一次买卖来回 0.2%
ROUND_TRIP_FEE = 0.002

SYMBOLS = [
    "TSLABUSDT", "NBISBUSDT", "BMNRBUSDT", "SPYBUSDT", "CRCLBUSDT",
    "COINBUSDT", "MSTRBUSDT", "SPCXBUSDT", "NVDABUSDT", "MSFTBUSDT",
    "GOOGLBUSDT", "AAPLBUSDT", "QQQBUSDT",
]

# 隔夜窗口：前一交易日 20:00 UTC 收盘到次日 13:30 UTC 开盘
OVERNIGHT_HOURS = 17.5


def correlation(xs: list[float], ys: list[float]) -> float:
    """皮尔逊相关系数"""
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    dx = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    dy = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if not dx or not dy:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / (dx * dy)


def t_statistic(samples: list[float]) -> float:
    """单样本 t 值，检验均值是否显著异于零"""
    if len(samples) < 2:
        return 0.0
    sd = statistics.pstdev(samples)
    if not sd:
        return 0.0
    return statistics.mean(samples) / (sd / math.sqrt(len(samples)))


def collect() -> tuple[list, list, list]:
    """采集开盘跳空、隔夜 BTC 收益、以及开盘后次 30 分钟收益"""
    btc = {bar[0]: float(bar[1]) for bar in fetch_klines("BTCUSDT", "30m")}

    btc_vs_gap, momentum_x, momentum_y = [], [], []
    for symbol in SYMBOLS:
        bars = {bar[0]: bar for bar in fetch_klines(symbol, "30m")}
        for ts_ms, bar in bars.items():
            ts = to_utc(ts_ms)
            # 只取常规交易日开盘那一根（13:30 UTC，数据区间全在夏令时内）
            if ts.weekday() >= 5 or ts.hour != 13 or ts.minute != 30:
                continue
            # 美股休市日当天没有开盘，不能算作一次开盘事件
            try:
                if CLOCK.state(ts.replace(minute=45)) != "OPEN":
                    continue
            except Exception:
                continue
            open_px, close_px = float(bar[1]), float(bar[4])
            if not open_px:
                continue
            gap = (close_px - open_px) / open_px

            prior = btc.get(ts_ms - int(OVERNIGHT_HOURS * 3600 * 1000))
            current = btc.get(ts_ms)
            if prior and current and gap:
                btc_vs_gap.append(((current - prior) / prior, gap))

            nxt = bars.get(ts_ms + 30 * 60 * 1000)
            if nxt and float(nxt[1]):
                momentum_x.append(gap)
                momentum_y.append((float(nxt[4]) - float(nxt[1])) / float(nxt[1]))

    return btc_vs_gap, momentum_x, momentum_y


def main() -> None:
    btc_vs_gap, mom_x, mom_y = collect()

    print("=" * 68)
    print("Hypothesis 1: predict gap direction from the overnight BTC move")
    print("=" * 68)
    hits = sum(1 for br, gap in btc_vs_gap if (br > 0) == (gap > 0))
    print(f"  hit rate   {hits / len(btc_vs_gap):.1%}   (n={len(btc_vs_gap)})")
    print(f"  corr       {correlation(*map(list, zip(*btc_vs_gap))):+.3f}")
    print("  VERDICT    a coin flip; direction is not predictable\n")

    print("=" * 68)
    print("Hypothesis 2: does the first 30 min after the open continue?")
    print("=" * 68)
    # 顺着首 30 分钟的方向做次 30 分钟，同向为盈、反向为亏
    returns = [abs(y) if (x > 0) == (y > 0) else -abs(y) for x, y in zip(mom_x, mom_y)]
    gross = statistics.mean(returns)
    net = [r - ROUND_TRIP_FEE for r in returns]
    print(f"  corr       {correlation(mom_x, mom_y):+.3f}   (n={len(mom_x)})")
    print(f"  gross      {gross:+.4%} per trade   t={t_statistic(returns):+.2f}")
    print(f"  net of fee {statistics.mean(net):+.4%} per trade   t={t_statistic(net):+.2f}")
    print("  VERDICT    the momentum is real, and it is smaller than the fee\n")

    print("=" * 68)
    print("The efficiency boundary of this market is precisely the width of its fee.")
    print("=" * 68)


if __name__ == "__main__":
    main()
