# Reproducing Opening Bell from zero

Written after doing it once. Every trap below is one we actually hit — several of them
fail **silently**, which is the worst way for an unattended overnight agent to fail.

**Time required:** ~40 minutes, plus one manual funding step that only you can do.
**Minimum capital:** 30 USDT to see it work end to end; ~180 USDT to reproduce our
four-symbol equal-risk configuration.

---

## Part 1 — Analysis only (no account, no money, 5 minutes)

Every claim in the README is verifiable without an account. Binance market data is
public and unauthenticated.

```bash
git clone https://github.com/fangTiger/bn-agent-hack.git && cd bn-agent-hack
python3 -m pip install pytest matplotlib
python3 -m pytest tests/ -v            # 18 tests, including the Labor Day assertion
python3 scripts/cohort_stats.py        # the §1 and §4 tables, with cohort filtering shown
python3 scripts/falsify.py             # the two falsification tests from README §3
python3 scripts/make_figures.py        # regenerates both figures in docs/figures/
```

`falsify.py` is the one to run if you are sceptical of the central claim. It should
print a ~50% hit rate for BTC-based direction prediction, and post-open momentum of
roughly +0.045% gross against a 0.2% round-trip fee.

Requires Python ≥ 3.11 (uses `zoneinfo` and `X | Y` type syntax) and matplotlib for
the figures. Nothing else.

> **Trap 1 — Binance kline pagination silently truncates history.**
> Calling `/api/v3/klines` *without* `startTime` returns the **most recent** 1000
> candles, not the oldest. A naive `while` loop then exits after one batch. We lost
> 50% of our sample this way and only noticed because the Monday count looked wrong.
> Always start from `startTime=0` and roll forward. See `fetch_klines()` in
> `src/calibrate.py`.

## Part 2 — Connect the agent to Binance

### 2.1 Register the MCP server

```bash
claude mcp add binance-mcp-server --transport http https://agent.binance.com/mcp/agentic
```

Then run `/mcp` inside Claude Code and complete the OAuth flow. Grant **Spot** scope.

> **Trap 2 — a server added mid-session does not appear in `/mcp`.**
> You must restart Claude Code **in the same directory** before the new server shows up.

> **Trap 3 — MCP authorisation does not extend to subdirectories.**
> The server is registered with *local scope*, bound to the exact directory you ran
> `claude mcp add` in. Running `claude -p` from a child directory returns
> *"binance MCP tools are unavailable"* — with no hint about why. Either register it in
> the directory you will run from, or `cd` to the authorised parent first
> (this is what `ops/tick.sh` does).

Verify:

```bash
claude -p "Use the binance MCP spot_getAccount tool and print only the USDT balance." \
  --allowedTools "mcp__binance-mcp-server__spot_getAccount"
```

### 2.2 Fund the Agentic sub-account — you must do this by hand

The agent has `enableWithdrawals: false` and cannot move funds in. Transfer USDT
yourself at
<https://www.binance.com/en/my/sub-account/asset-management/transfer>, choosing the
Agentic sub-account and the **Spot** wallet.

> **Trap 4 — bstock is quoted in USDT only.** `TSLABUSDC` does not exist (`-1121
> Invalid symbol`). If you funded with USDC, convert first — the agent can do this
> itself via `convert_sendQuoteRequest` → `convert_acceptQuote`. Measured rate
> USDC→USDT was 0.999919 (0.008% cost), materially better than paying 0.1% spot fees.

### 2.3 Verify the write path **before** building anything else

This is the one check that must not wait. If protective stop orders are rejected on
bstock, the whole design collapses — and you want to know on day 1, not day 4.

```
1. spot_newOrder  BUY MARKET  TSLABUSDT  quantity=0.015     (~5 USDT)
2. spot_newOrder  SELL STOP_LOSS_LIMIT  quantity=0.014
                  stopPrice=<mid × 0.93>  price=<mid × 0.92>  timeInForce=GTC
3. spot_deleteOrder  <orderId>
```

Step 2 succeeding is the green light for everything else.

> **Trap 5 — `STOP_LOSS_LIMIT` notional is checked against the LIMIT leg, not the
> market price.** `minNotional` is 5 USDT, and `0.015 × 326 = 4.89` gets rejected with
> `-1013 Filter failure: NOTIONAL` even though the position is worth 5.30 at market.
> **The further out your stop, the larger the minimum position it requires:**
> `min_qty ≥ 5 / (mid × (1 − stop_distance − buffer))`.
> This bites hardest on high-volatility symbols: NBISB's p90 is 7.6%, so its limit leg
> sits ~9.6% below mid and it needs a ~5.5 USDT position minimum.
> Also note `applyMinToMarket=true` — plain market orders are subject to the same
> 5 USDT floor, so you cannot "top up" a position with a 1 USDT buy.

## Part 3 — Automation

```bash
crontab -e
# */15 * * * * /abs/path/to/bn-ai-2/ops/tick.sh >> /abs/path/to/bn-ai-2/log/cron.log 2>&1
```

**Never encode market hours in cron.** `30 13 * * 1-5` fires on Labor Day for an
opening that does not exist — the exact bug this project exists to criticise. The
schedule is unconditional; `src/market_clock.py` decides whether to act.

> **Trap 6 — three separate reasons `claude -p` dies under cron, all silent.**
> Reproduce them all before trusting an unattended run:
> ```bash
> env -i HOME=$HOME /bin/bash ops/tick.sh
> ```
> 1. **`PATH` is minimal** → `claude` and `python3` are not found.
> 2. **`claude` may be a shell alias.** On this machine `~/.zshrc` aliases it with
>    proxy variables; cron does not read `.zshrc`, so the real binary runs without a
>    proxy and cannot reach the API. Use the absolute path and export the proxy vars.
> 3. **Missing `USER`/`LOGNAME` → `Not logged in`.** Credentials live in the macOS
>    Keychain (`security find-generic-password -s "Claude Code-credentials"`), and
>    reading them needs the identity variables. This is the nastiest one: it reports
>    an auth error, not an environment error, sending you off to re-run `/login`
>    for no reason.
>
> All three are handled at the top of `ops/tick.sh`.

> **Trap 7 — `crontab <path>` truncates long paths.** It silently dropped the last
> character of our scratch path. Pipe it instead: `crontab - <<EOF ... EOF`.

Confirm cron actually runs — installing is not the same as executing:

```bash
# temporarily set the entry to * * * * *, wait two minutes, then:
cat log/cron.log
# expect: [<ts>] MARKET_CLOSED — outside the pre-close guard window, no action needed
```

## Part 4 — What a real run produces

At `next_close − 15min`, `tick.sh` wakes the agent, which:

1. reads positions and prices over MCP,
2. writes `state/input.json`,
3. runs `python3 -m src.decide --input state/input.json --output log/decision_<date>.json`,
4. places the resulting orders and verifies each with `spot_getOrder`,
5. appends receipts to `log/execution_<date>.jsonl`.

Everything is idempotent on `(session_close, symbol)`. Running twice yields:

```
A decision already exists for this session close and symbol; refusing duplicate
```

> **Trap 8 — do not point shell redirection at the decision log.**
> `decide.py` uses `log/decide_<date>.log` as its **JSONL idempotency ledger**. We
> briefly redirected stderr into that same filename; one line of argparse output
> corrupted it, and the agent — correctly — abstained on every symbol rather than risk
> a duplicate order. Correct behaviour, self-inflicted cause.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `binance MCP tools are unavailable` | Wrong directory (Trap 3), or restart needed (Trap 2) |
| `Not logged in` under cron | Missing `USER`/`LOGNAME` (Trap 6.3) |
| `-1013 Filter failure: NOTIONAL` | Notional computed on the limit leg (Trap 5) |
| `-1121 Invalid symbol` | bstock is USDT-quoted only (Trap 4) |
| `-1003 Too much request weight` | Weight limit is 6000/min. A 15-minute cadence is nowhere near it; we hit it only by hand-testing in bursts |
| Every symbol returns `ABSTAIN` | Check the reason string — it always says which precondition failed |
| Agent runs but does nothing | Expected outside the guard window. `MARKET_CLOSED` in `cron.log` is success, not failure |

## Verification checklist

- [ ] `pytest tests/ -v` → 18 passed
- [ ] `python3 -m src.decide --dry-run` → four symbols, four different stop distances
- [ ] `STOP_LOSS_LIMIT` placed and cancelled manually (§2.3)
- [ ] `env -i HOME=$HOME /bin/bash ops/tick.sh` → runs without a populated environment
- [ ] cron probe confirms the daemon executes
- [ ] Calibrated p90 values differ by an order of magnitude across symbols — if they
      are all similar, calibration is broken
