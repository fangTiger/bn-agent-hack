#!/usr/bin/env python3
"""生成 Track A 演示用的核心图表

产出（docs/figures/）：
  fig1_holiday.png   因果证据：美股休市日，开盘尖峰从不出现（自然实验）
  fig2_spectrum.png  staleness 光谱：标的有无「盘外定价场所」决定它休市时有多死

设计取向：深色背景、大字号，为视频录屏优化。

注：本脚本用固定的 13:30 UTC 作为开盘时刻，仅因数据范围（2026-06 至 09）全部
落在美东夏令时内。生产代码必须用 src/market_clock.py 推导，不得沿用此简化。
"""

import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.calibrate import fetch_klines, to_utc  # noqa: E402
from src.calibrate import calibrate  # noqa: E402
from src.market_clock import MarketClock  # noqa: E402

FIG_DIR = ROOT / "docs" / "figures"

BG, FG, MUTED = "#0d1117", "#e6edf3", "#7d8590"
HOT, COOL, ACCENT = "#ff6b4a", "#4ac4ff", "#ffd33d"

plt.rcParams.update({
    "font.sans-serif": ["Arial Unicode MS", "Songti SC"],
    "axes.unicode_minus": False,
    "figure.facecolor": BG, "axes.facecolor": BG, "savefig.facecolor": BG,
    "text.color": FG, "axes.labelcolor": FG,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.edgecolor": "#30363d", "font.size": 13,
})

# 2026 年落在数据范围内的美股休市日（六月节、独立日顺延），均为周五
HOLIDAYS = {"2026-06-19", "2026-07-03"}

# 分组依据：标的在美股休市期间有无仍在交易的定价场所
GROUPS = {
    "无盘外定价场所（纯个股）": (
        ["TSLABUSDT", "AAPLBUSDT", "NVDABUSDT", "MSFTBUSDT", "GOOGLBUSDT",
         "NBISBUSDT", "SPCXBUSDT"], HOT),
    "有加密市场作为代理": (
        ["CRCLBUSDT", "COINBUSDT", "MSTRBUSDT", "HOODBUSDT", "BMNRBUSDT"], COOL),
    "有股指期货作为代理": (["SPYBUSDT", "QQQBUSDT"], MUTED),
}


def opening_samples(symbol):
    """返回 (正常交易日开盘30min波动, 休市日同一时刻波动, 休市时段30min波动)"""
    norm, holi, closed = [], [], []
    for k in fetch_klines(symbol, "30m"):
        ts = to_utc(k[0])
        o, c = float(k[1]), float(k[4])
        if not o:
            continue
        r = abs((c - o) / o)
        wd, hh, mm = ts.weekday(), ts.hour, ts.minute
        is_holiday = ts.strftime("%Y-%m-%d") in HOLIDAYS
        if wd < 5 and hh == 13 and mm == 30:
            (holi if is_holiday else norm).append(r)
        elif is_holiday or not (wd < 5 and (13, 30) <= (hh, mm) < (20, 0)):
            closed.append(r)
    return norm, holi, closed


def fig1_holiday():
    """因果证据图：休市日的同一时刻，开盘尖峰消失"""
    syms = ["TSLABUSDT", "NVDABUSDT", "MSFTBUSDT", "CRCLBUSDT",
            "MSTRBUSDT", "SPCXBUSDT", "QQQBUSDT"]
    norm_all, holi_all, labels, nv, hv = [], [], [], [], []
    for s in syms:
        n, h, _ = opening_samples(s)
        if not n or not h:
            continue
        labels.append(s.replace("BUSDT", ""))
        nv.append(statistics.mean(n) * 100)
        hv.append(statistics.mean(h) * 100)
        norm_all += n
        holi_all += h

    fig, ax = plt.subplots(figsize=(13, 7))
    x = range(len(labels))
    w = 0.38
    ax.bar([i - w / 2 for i in x], nv, w, color=HOT, label="Regular trading days")
    ax.bar([i + w / 2 for i in x], hv, w, color=MUTED, label="US market holidays (same clock time)")
    ax.set_xticks(list(x), labels)
    ax.set_ylabel("Price move in the 30 min after the open (%)")

    N = statistics.mean(norm_all) * 100
    H = statistics.mean(holi_all) * 100
    ax.axhline(N, color=HOT, ls=":", lw=1.4, alpha=0.7)
    ax.axhline(H, color=MUTED, ls=":", lw=1.4, alpha=0.7)
    ax.annotate(f"mean {N:.2f}%  (n={len(norm_all)})",
                xy=(len(labels) - 0.4, N), xytext=(0, 8),
                textcoords="offset points", color=HOT, fontsize=13, ha="right")
    ax.annotate(f"mean {H:.2f}%  (n={len(holi_all)})",
                xy=(len(labels) - 0.4, H), xytext=(0, 8),
                textcoords="offset points", color=MUTED, fontsize=13, ha="right")

    ax.set_title(f"The spike is caused by the OPENING, not by the clock \u2014 {N / H:.1f}x difference",
                 color=FG, fontsize=18, pad=44, loc="left")
    ax.text(0, 1.015, "Tokens trade 24/7. On days the underlying market never opened, that spike never appears.",
            transform=ax.transAxes, color=MUTED, fontsize=13.5)
    ax.legend(frameon=False, fontsize=13.5, loc="upper left")
    ax.grid(axis="y", color="#21262d", lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    out = FIG_DIR / "fig1_holiday.png"
    fig.savefig(out, dpi=160)
    print("已生成", out, f"| 交易日 {N:.3f}% vs 休市日 {H:.3f}% = {N / H:.1f}x")


def fig2_stoploss():
    """核心演示图：同一份代码，对四个标的算出差异巨大的止损距离"""
    # 必须走生产标定，而不是本文件另算一份：图上写着「同一份代码」，
    # 若用简单收益+离散分位数，就会和 README 紧邻的表格对不上。
    demo = ["SPYBUSDT", "BMNRBUSDT", "TSLABUSDT", "NBISBUSDT"]
    clock = MarketClock()
    rows = []
    for s in demo:
        profile = calibrate(s, clock)
        _, _, closed = opening_samples(s)
        rows.append((s.replace("BUSDT", ""), profile.p90,
                     statistics.mean(closed), profile.n_samples))
    rows.sort(key=lambda r: r[1])

    fig, ax = plt.subplots(figsize=(13, 7))
    ypos = list(range(len(rows)))
    vals = [r[1] * 100 for r in rows]
    # 止损距离越大 = 该标的休市期风险越高，用暖色深浅表达
    colors = [MUTED, COOL, HOT, "#ff3b30"][:len(rows)]
    ax.barh(ypos, vals, color=colors, height=0.6)
    ax.set_yticks(ypos, [r[0] for r in rows], fontsize=16)

    base = vals[0]
    for i, (lbl, p90, cm, n) in enumerate(rows):
        ax.annotate(f"  {p90:.2%}   ({p90 * 100 / base:.0f}x {rows[0][0]}, n={n})",
                    xy=(vals[i], i), va="center", color=FG, fontsize=14)

    ax.set_xlim(0, max(vals) * 1.55)
    ax.set_xlabel("Stop distance computed by the agent = 90th percentile of that symbol's own opening gaps")
    ax.set_title("Same code, same run \u2014 four symbols, stop distances "
                 f"{vals[-1] / vals[0]:.0f}x apart",
                 color=FG, fontsize=18, pad=44, loc="left")
    ax.text(0, 1.015, "Each distance is measured from that symbol's own history \u2014 nothing is hard-coded",
            transform=ax.transAxes, color=MUTED, fontsize=13.5)
    ax.grid(axis="x", color="#21262d", lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    out = FIG_DIR / "fig2_stoploss.png"
    fig.savefig(out, dpi=160)
    print("已生成", out, "|", " ".join(f"{r[0]}={r[1]:.2%}" for r in rows))


if __name__ == "__main__":
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig1_holiday()
    fig2_stoploss()
