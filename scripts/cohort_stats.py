#!/usr/bin/env python3
"""产出 README §1 与 §4 的统计口径，使其可被独立复现。

此前这两节的数字由临时脚本算出、未落盘，导致一个筛选缺陷长期未被发现：
规则 `baseAsset.endswith("B")` 会把 SHIB（柴犬币）当成代币化美股收进样本，
它一个标的贡献了 37.8% 的观测，且历史回溯到 2021 年——远早于 bstock 上线。

现在改用两道过滤：命名形态 + 上市时间。代币化美股均于 2026-06 之后上线，
任何在此之前就有 K 线的标的必然不是 bstock。
"""

import json
import statistics
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gap_analysis import fetch_klines, to_utc  # noqa: E402
from src.market_clock import MarketClock  # noqa: E402

CLOCK = MarketClock()

# bstock 于 2026-06 起陆续上线；早于此的标的一律不是代币化美股
BSTOCK_LISTED_AFTER = datetime(2026, 5, 1, tzinfo=timezone.utc)
MIN_OPENING_SAMPLES = 20
OPEN_HOUR, OPEN_MINUTE = 13, 30   # 美股 09:30 ET，数据区间全在夏令时内
CLOSE_HOUR = 20


def candidate_symbols() -> list[str]:
    """按命名形态取候选：标的代码 + B 后缀，以 USDT 计价"""
    url = "https://api.binance.com/api/v3/exchangeInfo"
    info = json.load(urllib.request.urlopen(url, timeout=30))
    return sorted(
        s["symbol"] for s in info["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
        and s["baseAsset"].endswith("B") and 4 <= len(s["baseAsset"]) <= 7
    )


def measure(symbol: str):
    """返回 (休市时段波动, 开盘后30分钟波动, 周一开盘, 周二至周五开盘, 首根K线时间)"""
    bars = fetch_klines(symbol, "30m")
    if not bars:
        return None
    closed, post, monday, other = [], [], [], []
    for bar in bars:
        ts = to_utc(bar[0])
        open_px, close_px = float(bar[1]), float(bar[4])
        if not open_px:
            continue
        move = abs((close_px - open_px) / open_px)
        weekday, hour, minute = ts.weekday(), ts.hour, ts.minute
        # 休市日（如六月节、独立日）当天没有开盘，那一根不能计入开盘事件——
        # 它正是 README §2 用作对照组的窗口。
        # 日历只覆盖 2026-2027；越界者必然不是 bstock，按非开盘处理，随后会被上市时间过滤剔除。
        if weekday < 5 and hour == OPEN_HOUR and minute == OPEN_MINUTE:
            try:
                really_open = CLOCK.state(ts.replace(minute=45)) == "OPEN"
            except Exception:
                really_open = False
        else:
            really_open = False

        if really_open:
            post.append(move)
            (monday if weekday == 0 else other).append(move)
        elif not (weekday < 5 and (OPEN_HOUR, OPEN_MINUTE) <= (hour, minute) < (CLOSE_HOUR, 0)):
            closed.append(move)
    return closed, post, monday, other, to_utc(bars[0][0])


def main() -> None:
    ratios, monday_all, other_all, kept, rejected = [], [], [], [], []

    for symbol in candidate_symbols():
        result = measure(symbol)
        if result is None:
            continue
        closed, post, monday, other, first_bar = result

        if first_bar < BSTOCK_LISTED_AFTER:
            rejected.append((symbol, first_bar, len(post)))
            continue
        if len(post) < MIN_OPENING_SAMPLES or not closed or statistics.mean(closed) == 0:
            continue

        ratios.append(statistics.mean(post) / statistics.mean(closed))
        monday_all += monday
        other_all += other
        kept.append(symbol)

    if rejected:
        print("已剔除（上市时间早于 bstock，不是代币化美股）：")
        for symbol, first_bar, n in rejected:
            print(f"  {symbol:<12} 首根 K 线 {first_bar:%Y-%m-%d}，本会贡献 {n} 个开盘观测")
        print()

    monday_mean = statistics.mean(monday_all)
    other_mean = statistics.mean(other_all)

    print("README §1 —— 休市 vs 开盘")
    print(f"  symbols            {len(kept)}")
    print(f"  opening events     {len(monday_all) + len(other_all)}")
    print(f"  stale ratio median {statistics.median(ratios):.1f}x")
    print(f"  range              {min(ratios):.1f}x - {max(ratios):.1f}x")
    print()
    print("README §4 —— 风险以开盘次数计，而非小时数")
    print(f"  Monday (after 65.5h closed)     {monday_mean:.3%}   n={len(monday_all)}")
    print(f"  Tuesday-Friday (after 17.5h)    {other_mean:.3%}   n={len(other_all)}")
    print(f"  ratio                           {monday_mean / other_mean:.2f}x")
    print(f"  square-root-of-time model would predict {(65.5 / 17.5) ** 0.5:.2f}x")


if __name__ == "__main__":
    main()
