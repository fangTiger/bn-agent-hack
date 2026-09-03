#!/usr/bin/env bash
# Opening Bell —— cron 入口
#
# 关键设计：这个脚本对「市场几点开盘」一无所知，也绝不能知道。
# cron 每 15 分钟无条件唤醒它一次；要不要做事、现在处于哪个状态，
# 完全由 src/market_clock.py 判断。
#
# 本项目存在的理由就是批判「把市场时间硬编码进调度器」这个 bug
# （例如 `30 13 * * 1-5`）——那样的 crontab 在 2026-09-07 劳动节
# 会触发一次不存在的开盘。所以这里只能是 */15 * * * *。
#
# 安装：
#   crontab -e
#   */15 * * * * /Users/captain/python/Claude/bn-ai-2/ops/tick.sh >> /Users/captain/python/Claude/bn-ai-2/log/cron.log 2>&1

set -euo pipefail

# cron 不加载 ~/.zshrc，环境极简。两件事必须在这里显式还原，否则唤醒 agent 必然失败：
#   1) PATH —— cron 默认找不到 /opt/homebrew/bin/claude 与 /usr/local/bin/python3
#   2) 代理 —— 本机的 claude 是带代理前缀的 shell alias（见 ~/.zshrc:33），
#      不设代理则连不上 Anthropic API。注意这要求本地代理进程处于运行状态。
#   3) USER/LOGNAME —— claude 的登录凭据存在 macOS Keychain（Claude Code-credentials），
#      读取它需要用户身份变量；缺失时 claude 会报 "Not logged in" 而静默失败
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export USER="${USER:-$(id -un)}"
export LOGNAME="${LOGNAME:-$USER}"
export SHELL="${SHELL:-/bin/zsh}"
export http_proxy="http://127.0.0.1:10808"
export https_proxy="http://127.0.0.1:10808"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$https_proxy"
export no_proxy="localhost,127.0.0.1,::1,.local"
export NO_PROXY="$no_proxy"
CLAUDE_BIN="/opt/homebrew/bin/claude"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p log

TS="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
DAY="$(date -u '+%Y-%m-%d')"

# 第一步：纯时间判断。只依赖 MarketClock，不需要任何账户数据、不产生任何网络写请求。
# 唯一需要行动的窗口是「底层市场收盘前 15 分钟」——过了这个点，敞口就要过夜或过周末。
WINDOW="$(python3 - <<'PY'
from datetime import datetime, timezone
from src.market_clock import MarketClock

now = datetime.now(timezone.utc)
clock = MarketClock()
if clock.state(now) != "OPEN":
    print("MARKET_CLOSED")
else:
    seconds_left = (clock.next_close(now) - now).total_seconds()
    print("GUARD_WINDOW" if 0 < seconds_left <= 900 else "MARKET_OPEN")
PY
)"

if [ "$WINDOW" != "GUARD_WINDOW" ]; then
    echo "[$TS] $WINDOW —— 不在收盘前守护窗口内，无需行动"
    exit 0
fi

# 第二步：有事要做，才唤醒 agent 执行。
# Claude Code 是这里唯一能调用 Binance MCP 的执行者——
# 本项目走 OAuth 授权，没有 API key，Python 侧无法自行下单。
#
# 坑（已实测）：Binance MCP 以 local scope 注册在父目录 $MCP_ROOT 上，
# 子目录不继承该授权——在 bn-ai-2/ 下运行 claude 会得到
# 「binance MCP 工具当前不可用」。因此必须切到 $MCP_ROOT 运行，
# 并在 prompt 内全部使用绝对路径。
MCP_ROOT="$(dirname "$ROOT")"

echo "[$TS] 存在待执行决策，唤醒 agent"
(
  cd "$MCP_ROOT"
  "$CLAUDE_BIN" -p "$(cat "$ROOT/ops/agent_prompt.md")" \
      --allowedTools \
        "mcp__binance-mcp-server__spot_newOrder,mcp__binance-mcp-server__spot_getOrder,mcp__binance-mcp-server__spot_getAccount,mcp__binance-mcp-server__spot_getOpenOrders,Read,Write"
) >> "$ROOT/log/agent_${DAY}.log" 2>&1

echo "[$TS] tick 完成"
