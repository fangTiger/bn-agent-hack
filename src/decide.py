"""根据市场时钟、跳空画像和交易所过滤规则生成纯计算决策。"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Literal, Mapping

from src.calibrate import GapProfile, calibrate, fetch_klines
from src.market_clock import MarketClock


LOG_DIR = Path(__file__).resolve().parent.parent / "log"
MAX_PRICE_AGE = timedelta(minutes=5)
MIN_PROFILE_SAMPLES = 20
RISK_BUDGET_RATE = 0.015
MIN_NOTIONAL = Decimal("5")
SELL_PRICE_FLOOR = Decimal("0.9")
SLIPPAGE_BUFFER = Decimal("0.02")
DRY_RUN_SYMBOLS = ("TSLABUSDT", "NBISBUSDT", "BMNRBUSDT", "SPYBUSDT")


@dataclass
class Decision:
    action: Literal["TRIM", "PLACE_STOP", "HOLD", "ABSTAIN"]
    symbol: str
    quantity: float | None
    stop_price: float | None
    limit_price: float | None
    reason: str
    risk_before: float
    risk_after: float


def pre_close_guard(
    now: datetime,
    positions: Mapping[str, Mapping[str, Any]],
    prices: Mapping[str, Mapping[str, Any]],
    profiles: Mapping[str, GapProfile],
    filters: Mapping[str, Mapping[str, Any]],
    equity: float,
    clock: MarketClock,
) -> list[Decision]:
    """为每个持仓生成一次收盘前风险决策，并写入幂等决策日志。"""
    symbols = sorted(positions)
    if not symbols:
        return []

    try:
        instant = _as_utc(now)
        if clock.state(instant) != "OPEN":
            return _abstain_all(symbols, "Not in US regular trading hours; skipping pre-close guard")
        session_close = clock.next_close(instant)
        next_open = clock.next_open(instant)
        openings = clock.openings_between(instant, next_open)
        if openings <= 0:
            return _abstain_all(symbols, "Market clock found no upcoming open; abstaining")
    except Exception as exc:
        return _abstain_all(symbols, f"Market clock could not resolve the next open: {exc}")

    log_path = LOG_DIR / f"decide_{session_close.date().isoformat()}.log"
    try:
        existing_keys = _read_decision_keys(log_path)
    except (OSError, ValueError) as exc:
        return _abstain_all(symbols, f"Decision log unreadable, idempotency cannot be guaranteed: {exc}")

    try:
        total_budget = float(equity) * RISK_BUDGET_RATE
    except (TypeError, ValueError):
        total_budget = 0.0
    if not math.isfinite(total_budget) or total_budget <= 0:
        return _abstain_all(symbols, "Invalid account equity; cannot derive risk budget")
    symbol_budget = total_budget / len(symbols)

    decisions: list[Decision] = []
    for symbol in symbols:
        key = (session_close.isoformat(), symbol)
        if key in existing_keys:
            decisions.append(
                _abstain(symbol, "A decision already exists for this session close and symbol; refusing duplicate")
            )
            continue

        decision = _decide_symbol(
            symbol=symbol,
            position=positions[symbol],
            price=prices.get(symbol),
            profile=profiles.get(symbol),
            symbol_filters=filters.get(symbol),
            now=instant,
            openings=openings,
            symbol_budget=symbol_budget,
        )
        try:
            _append_decision(log_path, session_close, decision)
        except OSError as exc:
            decision = Decision(
                action="ABSTAIN",
                symbol=symbol,
                quantity=None,
                stop_price=decision.stop_price,
                limit_price=decision.limit_price,
                reason=f"Failed to write decision log; aborting execution: {exc}",
                risk_before=decision.risk_before,
                risk_after=decision.risk_before,
            )
        decisions.append(decision)
    return decisions


def _decide_symbol(
    *,
    symbol: str,
    position: Mapping[str, Any],
    price: Mapping[str, Any] | None,
    profile: GapProfile | None,
    symbol_filters: Mapping[str, Any] | None,
    now: datetime,
    openings: int,
    symbol_budget: float,
) -> Decision:
    """生成单标的决策。"""
    try:
        quantity = float(position["quantity"])
    except (KeyError, TypeError, ValueError):
        return _abstain(symbol, "Position quantity missing or invalid; abstaining")
    if not math.isfinite(quantity) or quantity < 0:
        return _abstain(symbol, "Position quantity missing or invalid; abstaining")
    if quantity == 0:
        return Decision("HOLD", symbol, None, None, None, "No position held; nothing to do", 0.0, 0.0)

    if price is None:
        return _abstain(symbol, "Market data missing; abstaining")
    try:
        mid = float(price["mid"])
        weighted_avg = float(price["weighted_avg_5m"])
        price_time = _as_utc(price["timestamp"])
    except (KeyError, TypeError, ValueError):
        return _abstain(symbol, "Market data fields missing or invalid; abstaining")
    if (
        not math.isfinite(mid)
        or not math.isfinite(weighted_avg)
        or mid <= 0
        or weighted_avg <= 0
    ):
        return _abstain(symbol, "Market data fields missing or invalid; abstaining")
    if now - price_time > MAX_PRICE_AGE or price_time - now > MAX_PRICE_AGE:
        return _abstain(symbol, "Market data is stale; abstaining")

    if profile is None:
        return _abstain(symbol, "Gap calibration profile missing; abstaining")
    if profile.n_samples < MIN_PROFILE_SAMPLES:
        return _abstain(
            symbol,
            f"Insufficient calibration samples: {profile.n_samples} < {MIN_PROFILE_SAMPLES}; abstaining",
        )
    p90 = float(profile.p90)
    if not math.isfinite(p90) or not 0 < p90 < 1 - float(SLIPPAGE_BUFFER):
        return _abstain(symbol, "Calibrated p90 is invalid; abstaining")

    try:
        step_size, tick_size, min_notional = _parse_filters(symbol_filters)
    except ValueError as exc:
        return _abstain(symbol, f"Invalid exchange filters: {exc}")

    stress_rate = p90 * math.sqrt(openings)
    risk_before = quantity * mid * stress_rate
    # 超出预算但无法减仓时，退而求其次挂上保护止损——
    # 绝不能因为「减不动」就让整个敞口裸奔，那是用放弃保护来回避一个更小的问题。
    over_budget_note = ""
    if risk_before > symbol_budget:
        target_quantity = symbol_budget / (mid * stress_rate)
        trim_quantity = _round_down_quantity(quantity - target_quantity, step_size)
        if trim_quantity <= 0:
            over_budget_note = (
                f"Stress loss {risk_before:.4f} USDT slightly exceeds the equal-risk budget {symbol_budget:.4f} USDT, "
                "but the excess is smaller than one lot step, so no trim is possible; placing protective stop only"
            )
        elif trim_quantity * Decimal(str(mid)) < min_notional:
            over_budget_note = (
                f"Stress loss {risk_before:.4f} USDT exceeds the equal-risk budget {symbol_budget:.4f} USDT, "
                "but the trim notional falls below 5 USDT; placing protective stop only"
            )
        else:
            remaining = max(0.0, quantity - float(trim_quantity))
            risk_after = remaining * mid * stress_rate
            return Decision(
                "TRIM",
                symbol,
                float(trim_quantity),
                None,
                None,
                (
                    f"Stress loss {risk_before:.4f} USDT exceeds the equal-risk budget "
                    f"{symbol_budget:.4f} USDT; trimming down to budget"
                ),
                risk_before,
                risk_after,
            )

    order_quantity = _round_down_quantity(quantity, step_size)
    stop_price = _round_down_price(mid * (1 - p90), tick_size)
    limit_price = _round_down_price(
        mid * (1 - p90 - float(SLIPPAGE_BUFFER)), tick_size
    )
    if order_quantity <= 0 or stop_price <= 0 or limit_price <= 0:
        return Decision(
            "ABSTAIN",
            symbol,
            None,
            float(stop_price) if stop_price > 0 else None,
            float(limit_price) if limit_price > 0 else None,
            "Protective stop parameters became invalid after exchange rounding; abstaining",
            risk_before,
            risk_before,
        )

    limit_notional = order_quantity * limit_price
    if limit_notional < min_notional:
        required_quantity = _round_up_quantity(min_notional / limit_price, step_size)
        return Decision(
            "ABSTAIN",
            symbol,
            float(order_quantity),
            float(stop_price),
            float(limit_price),
            (
                "Protective stop limit leg is below the 5 USDT minimum notional; "
                f"at the current limit price at least {float(required_quantity):g} units are required; the stop distance must not be narrowed"
            ),
            risk_before,
            risk_before,
        )

    price_floor = Decimal(str(weighted_avg)) * SELL_PRICE_FLOOR
    if limit_price <= price_floor:
        return Decision(
            "ABSTAIN",
            symbol,
            float(order_quantity),
            float(stop_price),
            float(limit_price),
            "Protective stop limit hits the 0.9x five-minute average price floor; abstaining",
            risk_before,
            risk_before,
        )

    return Decision(
        "PLACE_STOP",
        symbol,
        float(order_quantity),
        float(stop_price),
        float(limit_price),
        (
            over_budget_note
            or f"Stress loss {risk_before:.4f} USDT is within the equal-risk budget "
               f"{symbol_budget:.4f} USDT; placing protective stop"
        ),
        risk_before,
        risk_before,
    )


def _parse_filters(
    symbol_filters: Mapping[str, Any] | None,
) -> tuple[Decimal, Decimal, Decimal]:
    """读取扁平过滤规则或币安 exchangeInfo 的过滤器列表。"""
    if symbol_filters is None:
        raise ValueError("exchange filters are missing")

    flattened: dict[str, Any] = dict(symbol_filters)
    raw_filters = symbol_filters.get("filters")
    if isinstance(raw_filters, list):
        by_type = {
            item.get("filterType"): item
            for item in raw_filters
            if isinstance(item, Mapping)
        }
        flattened = {
            "stepSize": by_type.get("LOT_SIZE", {}).get("stepSize"),
            "tickSize": by_type.get("PRICE_FILTER", {}).get("tickSize"),
            "minNotional": (
                by_type.get("NOTIONAL", {}).get("minNotional")
                or by_type.get("MIN_NOTIONAL", {}).get("minNotional")
            ),
        }
    try:
        step_size = Decimal(str(flattened["stepSize"]))
        tick_size = Decimal(str(flattened["tickSize"]))
        min_notional = max(
            MIN_NOTIONAL, Decimal(str(flattened.get("minNotional", MIN_NOTIONAL)))
        )
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("missing stepSize, tickSize or minNotional") from exc
    if step_size <= 0 or tick_size <= 0 or min_notional <= 0:
        raise ValueError("stepSize, tickSize and minNotional must be positive")
    return step_size, tick_size, min_notional


def _round_down_quantity(value: float, step_size: Decimal) -> Decimal:
    amount = Decimal(str(max(value, 0.0)))
    return (amount / step_size).to_integral_value(rounding=ROUND_DOWN) * step_size


def _round_up_quantity(value: Decimal, step_size: Decimal) -> Decimal:
    units = (value / step_size).to_integral_value(rounding=ROUND_DOWN)
    rounded = units * step_size
    return rounded if rounded >= value else (units + 1) * step_size


def _round_down_price(value: float, tick_size: Decimal) -> Decimal:
    amount = Decimal(str(value))
    return (amount / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size


def _read_decision_keys(log_path: Path) -> set[tuple[str, str]]:
    if not log_path.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    for line_number, line in enumerate(
        log_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            keys.add((str(record["session_close"]), str(record["symbol"])))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"line {line_number} is corrupted") from exc
    return keys


def _append_decision(
    log_path: Path, session_close: datetime, decision: Decision
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "session_close": session_close.isoformat(),
        "symbol": decision.symbol,
        "decision": asdict(decision),
    }
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")


def _abstain(symbol: str, reason: str) -> Decision:
    return Decision("ABSTAIN", symbol, None, None, None, reason, 0.0, 0.0)


def _abstain_all(symbols: list[str], reason: str) -> list[Decision]:
    return [_abstain(symbol, reason) for symbol in symbols]


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _dry_run() -> int:
    """用缓存行情和虚拟持仓展示四个标的的决策。"""
    clock = MarketClock()
    now = datetime(2026, 9, 4, 19, 45, tzinfo=timezone.utc)
    profiles: dict[str, GapProfile] = {}
    prices: dict[str, dict[str, Any]] = {}
    positions: dict[str, dict[str, float]] = {}

    exchange_info_path = Path(__file__).resolve().parent.parent / "data" / "exchangeinfo.json"
    exchange_info = json.loads(exchange_info_path.read_text(encoding="utf-8"))
    filters = {
        item["symbol"]: item
        for item in exchange_info.get("symbols", [])
        if item.get("symbol") in DRY_RUN_SYMBOLS
    }

    for symbol in DRY_RUN_SYMBOLS:
        try:
            profiles[symbol] = calibrate(symbol, clock)
            bars = fetch_klines(symbol, interval="30m")
            mid = float(bars[-1][4])
            prices[symbol] = {
                "mid": mid,
                "weighted_avg_5m": mid,
                "timestamp": now,
            }
            positions[symbol] = {"quantity": 100.0 / mid}
        except (OSError, RuntimeError, ValueError, IndexError) as exc:
            positions[symbol] = {"quantity": 1.0}
            print(f"{symbol}: public market data or calibration unavailable, will abstain; cause: {exc}")

    global LOG_DIR
    original_log_dir = LOG_DIR
    with tempfile.TemporaryDirectory(prefix="opening-bell-dry-run-") as temporary_dir:
        LOG_DIR = Path(temporary_dir)
        try:
            decisions = pre_close_guard(
                now,
                positions,
                prices,
                profiles,
                filters,
                equity=1000.0,
                clock=clock,
            )
        finally:
            LOG_DIR = original_log_dir

    for decision in decisions:
        print(json.dumps(asdict(decision), ensure_ascii=False, sort_keys=True))
    return 0


def _run_from_file(input_path: Path, output_path: Path) -> int:
    """从账户快照文件读取真实持仓，产出决策文件。

    本模块无法自行获取持仓（那需要认证接口），故由 Claude Code 经 MCP 查询后
    写入快照，本函数只负责纯计算。输入格式：

        {
          "now": "2026-09-03T19:50:00Z",        // 可选，缺省用当前时间
          "equity": 279.74,                      // 账户总权益 (USDT)
          "positions": {"TSLABUSDT": {"quantity": 0.117882}, ...},
          "prices": {"TSLABUSDT": {"mid": 356.78, "weighted_avg_5m": 356.70}, ...}
        }

    输出 {"generated_at": ..., "decisions": [...]}。
    """
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    clock = MarketClock()
    now = (_as_utc(datetime.fromisoformat(payload["now"].replace("Z", "+00:00")))
           if payload.get("now") else datetime.now(timezone.utc))

    positions = payload["positions"]
    symbols = sorted(positions)

    prices: dict[str, dict[str, Any]] = {}
    for symbol, entry in payload.get("prices", {}).items():
        item = dict(entry)
        # 快照未标注时间时，视为刚刚采集，由调用方保证新鲜度
        item.setdefault("weighted_avg_5m", item.get("mid"))
        item["timestamp"] = (
            _as_utc(datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00")))
            if item.get("timestamp") else now
        )
        prices[symbol] = item

    exchange_info_path = Path(__file__).resolve().parent.parent / "data" / "exchangeinfo.json"
    exchange_info = json.loads(exchange_info_path.read_text(encoding="utf-8"))
    filters = {
        item["symbol"]: item
        for item in exchange_info.get("symbols", [])
        if item.get("symbol") in set(symbols)
    }

    profiles: dict[str, GapProfile] = {}
    for symbol in symbols:
        try:
            profiles[symbol] = calibrate(symbol, clock)
        except (OSError, RuntimeError, ValueError, IndexError) as exc:
            # 标定失败不猜测，交由 pre_close_guard 输出 ABSTAIN
            print(f"{symbol}: calibration failed, will abstain; cause: {exc}")

    decisions = pre_close_guard(
        now, positions, prices, profiles, filters,
        equity=float(payload["equity"]), clock=clock,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({
        "generated_at": now.isoformat(),
        "next_open": clock.next_open(now).isoformat(),
        "skipped_reason": clock.skipped_reason(now, clock.next_open(now)),
        "decisions": [asdict(d) for d in decisions],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    for decision in decisions:
        print(json.dumps(asdict(decision), ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opening Bell pure-computation decision engine")
    parser.add_argument("--dry-run", action="store_true", help="demonstrate decisions with synthetic positions")
    parser.add_argument("--input", type=Path, help="account snapshot JSON (written by Claude Code over MCP)")
    parser.add_argument("--output", type=Path, help="path for the decision output JSON")
    arguments = parser.parse_args(argv)
    if arguments.dry_run:
        return _dry_run()
    if arguments.input and arguments.output:
        return _run_from_file(arguments.input, arguments.output)
    parser.error("specify --dry-run, or both --input and --output; this module never executes trades")


if __name__ == "__main__":
    raise SystemExit(main())
