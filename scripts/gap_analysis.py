#!/usr/bin/env python3
"""代币化美股（bstock）开盘跳空分析

核心假设：币安 bstock 7x24 交易，但标的真实美股每周仅开盘 32.5 小时。
周末与盘后的 bstock 价格由做市商挂出，不反映期间的信息面（stale price），
真实市场一开盘，累积的信息在短时间内被一次性 price in，形成跳空。

本脚本量化两个指标并对比：
    A. 周末漂移  = 周五美股收盘 -> 周一美股开盘前，bstock 自身走了多少
    B. 开盘冲击  = 周一开盘那一小时内，bstock 走了多少

若 |B| 显著大于 |A|，则 stale price 假设成立。

时区说明：2026 年美东夏令时区间为 3/8 - 11/1，本数据集（2026-06 起）全部落在
夏令时内，故美股开盘 09:30 ET = 13:30 UTC，收盘 16:00 ET = 20:00 UTC。
"""

import json
import statistics
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
API = "https://api.binance.com/api/v3/klines"

# 美股常规交易时段（UTC，夏令时）
MARKET_OPEN_HOUR = 13   # 13:30 UTC 开盘，落在 13:00-14:00 这根 1h K 线内
MARKET_CLOSE_HOUR = 20  # 20:00 UTC 收盘

SYMBOLS = ["TSLABUSDT", "SPCXBUSDT", "NBISBUSDT", "BMNRBUSDT"]


def fetch_klines(symbol: str, interval: str = "1h") -> list:
    """分段拉取指定交易对的全部历史 K 线，结果缓存到 data/ 目录。

    币安单次最多返回 1000 根，故按 startTime 向前滚动直到追平当前时间。
    """
    cache = DATA_DIR / f"{symbol}_{interval}.json"
    if cache.exists():
        return json.loads(cache.read_text())

    # 注意：不带 startTime 时币安返回「最近」1000 根，会导致只取到尾部数据。
    # 必须从 0 开始向后滚动，才能拿到该交易对的全部历史。
    bars, start = [], 0
    while True:
        url = f"{API}?symbol={symbol}&interval={interval}&limit=1000&startTime={start}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            batch = json.loads(resp.read())
        if not batch:
            break
        # 去掉与上一批重叠的第一根
        if bars and batch[0][0] == bars[-1][0]:
            batch = batch[1:]
        if not batch:
            break
        bars.extend(batch)
        if len(batch) < 999:
            break
        start = batch[-1][0] + 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(bars))
    return bars


def to_utc(ms: int) -> datetime:
    """毫秒时间戳转 UTC datetime"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def analyze(symbol: str) -> dict | None:
    """统计单个标的的开盘冲击与周末漂移"""
    bars = fetch_klines(symbol)
    # 以开盘时间为键建立索引，便于按时刻定位
    idx = {b[0]: b for b in bars}

    open_shocks = []     # 周一开盘那一小时的涨跌幅
    weekend_drifts = []  # 周五 20:00 收盘 -> 周一 13:00 开盘前的漂移
    rth_other = []       # 交易时段内除开盘首根外的 1h 涨跌幅
    closed_hours = []    # 休市时段（盘后 + 周末）的 1h 涨跌幅

    for b in bars:
        ts = to_utc(b[0])
        o, c = float(b[1]), float(b[4])
        if o == 0:
            continue
        ret = (c - o) / o

        # 交易时段：周一至周五 13:00-19:00 UTC（13:00 那根含 13:30 开盘）
        in_rth = ts.weekday() < 5 and MARKET_OPEN_HOUR <= ts.hour < MARKET_CLOSE_HOUR

        if ts.weekday() == 0 and ts.hour == MARKET_OPEN_HOUR:
            open_shocks.append((ts.strftime("%Y-%m-%d"), ret))
            # 上周五 19:00 那根的 close 即 20:00 收盘价，距周一 13:00 恰好 66 小时
            fri = idx.get(b[0] - 66 * 3600 * 1000)
            if fri:
                fri_c = float(fri[4])
                if fri_c:
                    weekend_drifts.append((o - fri_c) / fri_c)
        elif in_rth:
            rth_other.append(ret)
        else:
            closed_hours.append(ret)

    if not open_shocks:
        return None

    shock_vals = [abs(r) for _, r in open_shocks]
    rth_vals = [abs(r) for r in rth_other]
    closed_vals = [abs(r) for r in closed_hours]
    return {
        "symbol": symbol,
        "n_mondays": len(open_shocks),
        "shock_mean": statistics.mean(shock_vals),
        "shock_max": max(shock_vals),
        "rth_mean": statistics.mean(rth_vals),
        "closed_mean": statistics.mean(closed_vals),
        "closed_p95": sorted(closed_vals)[int(len(closed_vals) * 0.95)],
        "drift_mean": statistics.mean([abs(d) for d in weekend_drifts]) if weekend_drifts else None,
        "n_drifts": len(weekend_drifts),
        "detail": open_shocks,
    }


def main():
    print(f"{'标的':<11}{'周一':>5}{'开盘冲击':>10}{'开盘最大':>10}"
          f"{'交易时段':>10}{'休市时段':>10}{'周末漂移':>10}{'开盘/休市':>11}{'交易/休市':>11}")
    print("-" * 90)
    results = []
    for sym in SYMBOLS:
        r = analyze(sym)
        if not r:
            print(f"{sym:<11}  无足够数据")
            continue
        results.append(r)
        cm = r["closed_mean"]
        r1 = r["shock_mean"] / cm if cm else 0   # 开盘冲击相对休市时段的放大倍数
        r2 = r["rth_mean"] / cm if cm else 0     # 交易时段整体相对休市时段的放大倍数
        drift = f"{r['drift_mean']:.2%}" if r["drift_mean"] is not None else "n/a"
        print(f"{sym:<11}{r['n_mondays']:>5}{r['shock_mean']:>10.2%}{r['shock_max']:>10.2%}"
              f"{r['rth_mean']:>10.2%}{cm:>10.2%}{drift:>10}{r1:>10.1f}x{r2:>10.1f}x")

    print("\n各周一开盘那一小时的涨跌幅明细：")
    for r in results:
        print(f"\n  {r['symbol']}  （周末漂移样本 {r['n_drifts']} 个）")
        for date, ret in r["detail"]:
            bar = "#" * min(int(abs(ret) * 400), 60)
            print(f"    {date}  {ret:>+7.2%}  {bar}")


if __name__ == "__main__":
    main()
