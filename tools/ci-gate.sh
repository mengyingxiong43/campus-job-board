#!/bin/bash
# ci-gate.sh —— 每次改动都该跑一遍的两道闸门。
#
# 闸门一（任何机器都能跑，不需要浏览器）：
#   1. python3 applytracker.py --self-test   —— 纯 Python 自检，不需要 data/、不需要 config.ini
#   2. bash -n 三个 .command 启动脚本        —— 语法一错，双击就是闪退，肉眼很难发现
#   3. python3 -m py_compile applytracker.py
#
# 闸门二（要一个浏览器）：
#   4. 起一个**一次性**浏览器：safe-browser serve（临时 profile，绝不碰你的日常浏览器）
#   5. 干净度门禁：CDP 里必须只有 1 个空白页，且没有一个页面开着我们的看板。
#      为什么要有这道门禁：board-check.js 跑崩时不会关掉它开的页面（没有 try/finally），
#      残留标签页会留到下一轮 —— 它每 10 秒轮询一次看板，还会撞上「版本对不上自动刷新」，
#      于是下一轮的断言时红时不红。宁可在这里中止，也不要看一条看不懂的红。
#   6. 用 tools/make-demo-data.py 造一份**虚构**数据，起服务时 --base 和 --config 都指过去。
#      少了这两个参数，服务会读到仓库里的真实数据和真实飞书 token —— 这事踩过两次。
#   7. node tools/board-check.js
#   8. 失败项分拣：有任何 ✗ 都算新回归，一律 exit 1。
#      （曾有 tools/ci-known-fails.txt 已知假红名单；2026-10-08 演示数据补齐后
#      6 条假红全部消灭，名单已删 —— 现在门禁零容忍，退出码非 0 就不放行。）
#
# 用法：
#   bash tools/ci-gate.sh                  # 两道闸门都跑
#   SKIP_BROWSER=1 bash tools/ci-gate.sh   # 只跑闸门一（没浏览器的机器/CI 用）
#
# 环境变量：
#   BOARD_PORT   演示服务端口，默认 8801（**故意避开 8788–8791**，免得撞到作者真在跑的看板）
#   CDP_PORT     浏览器调试端口，默认 9455
#   PUPPETEER_DIR  puppeteer-core 所在 node_modules（不设就按 board-check.js 自己的顺序找）
#   CI_BROWSER_CMD  没有 safe-browser 时，用它起浏览器（必须自己带 --remote-debugging-port=$CDP_PORT
#                   和 --user-data-dir=<临时目录>），例：
#                   CI_BROWSER_CMD="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --headless=new"
#
# 退出码 0 = 放行；1 = 有失败项（能直接挂进 CI）。

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1

BOARD_PORT="${BOARD_PORT:-8801}"
CDP_PORT="${CDP_PORT:-9455}"

TMP="$(mktemp -d)"
BOARD_PID=""
BROWSER_PROFILE=""

# 并发锁：一台机器同时跑两轮 ci-gate 会互相残杀 —— 对方 cleanup 里的浏览器兜底
# 会把**我这轮正在用的浏览器**一起关掉，board-check 当场「detached Frame」崩掉
# （2026-10-08 实测：两个人同时跑，崩的那轮一行错都没解析出来）。
# 所以先抢锁，抢不到就明说，别排着队互相咬。锁目录带 pid，持有者死了算 stale，可安全接管。
LOCK="/tmp/at-ci-gate.lock"
if mkdir "$LOCK" 2>/dev/null; then
  echo "$$" > "$LOCK/pid"
  LOCKED=1
else
  OTHER="$(cat "$LOCK/pid" 2>/dev/null)"
  if [ -n "$OTHER" ] && ! kill -0 "$OTHER" 2>/dev/null; then
    rm -rf "$LOCK"                     # 上一个持有者已经死了，锁是 stale 的
    if mkdir "$LOCK" 2>/dev/null; then echo "$$" > "$LOCK/pid"; LOCKED=1; fi
  fi
  if [ -z "${LOCKED:-}" ]; then
    printf '\n  [中止] 另一轮 ci-gate 正在跑（pid %s）。等它跑完再来，或者：\n' "${OTHER:-?}"
    printf '         确认那边真的死了之后，rm -rf %s\n\n' "$LOCK"
    exit 1
  fi
fi

# safe-browser 不一定在 PATH 上（WorkBuddy 机器上通常在 ~/.workbuddy/bin/），找一下
SAFE_BROWSER=""
for cand in safe-browser "$HOME/.workbuddy/bin/safe-browser" /usr/local/bin/safe-browser; do
  if command -v "$cand" >/dev/null 2>&1; then SAFE_BROWSER="$(command -v "$cand")"; break; fi
  if [ -x "$cand" ]; then SAFE_BROWSER="$cand"; break; fi
done

cleanup() {
  [ -n "$BOARD_PID" ] && kill "$BOARD_PID" 2>/dev/null
  if [ -n "$SAFE_BROWSER" ] && [ -n "$BROWSER_PROFILE" ]; then
    "$SAFE_BROWSER" close "$BROWSER_PROFILE" >/dev/null 2>&1
    # 兜底：close 偶尔没杀干净（Edge 退出要几秒），残留实例会占住端口。
    # 但**只杀自己这个 profile 的进程**，绝不 close --all —— 那会顺手杀掉
    # 别人正在跑的那一轮的浏览器（2026-10-08 实测：两人同时跑，互相杀到崩溃）。
    if curl -sf -m 2 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
      pkill -f "user-data-dir=${BROWSER_PROFILE}" 2>/dev/null
      sleep 2
    fi
  fi
  rm -rf "$TMP"
  [ -n "${LOCKED:-}" ] && rm -rf "$LOCK"
}
trap cleanup EXIT INT TERM

say()  { printf '\n  %s\n' "$1"; }
ok()   { printf '  ✓ %s\n' "$1"; }
bad()  { printf '  ✗ %s\n' "$1"; }
die()  { printf '\n  [中止] %s\n\n' "$1"; exit 1; }

FAILED=0

# ---------------------------------------------------------------- 闸门一
printf '\n########## 闸门一：不依赖浏览器 ##########\n'

# 1) 自检
say "自检（python3 applytracker.py --self-test）"
if python3 applytracker.py --self-test > "$TMP/selftest.log" 2>&1; then
  ok "自检通过（$(grep -c '✓' "$TMP/selftest.log") 项）"
else
  bad "自检没通过"
  tail -20 "$TMP/selftest.log" | sed 's/^/      /'
  FAILED=1
fi

# 2) 启动脚本语法（.bat 只能静态检查，Windows 上才跑得起来）
say "启动脚本语法"
for f in start.command "开机自启.command" "关闭开机自启.command"; do
  if bash -n "$f"; then ok "bash -n 通过：$f"; else bad "bash -n 失败：$f"; FAILED=1; fi
done
# start.bat 是 Windows 批处理，macOS 上没有 cmd.exe，只能查「有没有明显的括号/编码坑」
if [ -f start.bat ]; then
  if grep -q "chcp 65001" start.bat; then ok "start.bat 已设 chcp 65001（中文不乱码）—— 只能静态查，真跑要 Windows"; else
    bad "start.bat 没有 chcp 65001"; FAILED=1; fi
fi

# 3) 主程序能编译
say "主程序编译"
if python3 -m py_compile applytracker.py; then ok "py_compile 通过"; else bad "py_compile 失败"; FAILED=1; fi

if [ "${SKIP_BROWSER:-0}" = "1" ]; then
  printf '\n  （SKIP_BROWSER=1，闸门二跳过）\n'
  [ "$FAILED" = "0" ] && printf '\n########## 闸门一通过 ##########\n\n' && exit 0
  printf '\n########## 闸门一有失败 ##########\n\n'; exit 1
fi

# ---------------------------------------------------------------- 闸门二
printf '\n########## 闸门二：浏览器体检 ##########\n'

# 上一次跑完/跑崩的实例可能还没死透、占着端口（Edge 退出要几秒）——
# 健康检查要是打到了旧实例上，后面全是对着幽灵测。端口被占就换一个，不跟将死的进程纠缠。
while curl -sf -m 2 "http://127.0.0.1:${BOARD_PORT}/api/health" >/dev/null 2>&1; do
  BOARD_PORT=$((BOARD_PORT + 1))
done
while curl -sf -m 2 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; do
  CDP_PORT=$((CDP_PORT + 1))
done

# 4) 演示数据（虚构公司，不碰真实数据）
say "造演示数据"
DEMO="$TMP/demo"
if python3 tools/make-demo-data.py --out-dir "$DEMO" > "$TMP/demo.log" 2>&1; then
  ok "演示数据已生成 → ${DEMO}/data"
else
  bad "演示数据生成失败"; tail -10 "$TMP/demo.log" | sed 's/^/      /'; exit 1
fi

# 5) 起服务（--base / --config 必须都给）
say "起演示服务（端口 ${BOARD_PORT}）"
python3 applytracker.py --serve --no-open --port "$BOARD_PORT" \
    --base "$DEMO" --config "$DEMO/config.ini" > "$TMP/board.log" 2>&1 &
BOARD_PID=$!
for _ in $(seq 1 30); do
  if curl -sf -m 2 "http://127.0.0.1:${BOARD_PORT}/api/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! curl -sf -m 2 "http://127.0.0.1:${BOARD_PORT}/api/health" >/dev/null 2>&1; then
  bad "演示服务没起来"; tail -15 "$TMP/board.log" | sed 's/^/      /'; exit 1
fi
ok "服务已起：$(curl -s -m 2 "http://127.0.0.1:${BOARD_PORT}/api/health")"

# 6) 一次性浏览器
say "起一次性浏览器（CDP 端口 ${CDP_PORT}）"
if [ -n "$SAFE_BROWSER" ]; then
  "$SAFE_BROWSER" serve --port "$CDP_PORT" --source ci > "$TMP/browser.log" 2>&1 &
elif [ -n "${CI_BROWSER_CMD:-}" ]; then
  # shellcheck disable=SC2086
  $CI_BROWSER_CMD --remote-debugging-port="$CDP_PORT" --user-data-dir="$TMP/chrome-profile" \
      about:blank > "$TMP/browser.log" 2>&1 &
else
  die "没有浏览器通道：本机没有 safe-browser，也没给 CI_BROWSER_CMD。
       只想跑不依赖浏览器的那一半：SKIP_BROWSER=1 bash tools/ci-gate.sh"
fi
for _ in $(seq 1 30); do
  curl -sf -m 2 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -sf -m 2 "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
  bad "浏览器没起来"; tail -10 "$TMP/browser.log" | sed 's/^/      /'; exit 1
fi
if [ -n "$SAFE_BROWSER" ]; then
  BROWSER_PROFILE="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("profile",""))
except Exception:
    print("")' "$TMP/browser.log")"
fi
ok "浏览器已起"

# 7) 干净度门禁
say "干净度门禁"
BROWSER_DIRTY=0
python3 - "$CDP_PORT" "$BOARD_PORT" <<'PY' || BROWSER_DIRTY=1
import json, sys, urllib.request
cdp, board = sys.argv[1], sys.argv[2]
d = json.load(urllib.request.urlopen("http://127.0.0.1:%s/json/list" % cdp, timeout=5))
pages = [t for t in d if t.get("type") == "page"]      # 后台页/扩展不算
print("    页面数 = %d" % len(pages))
for p in pages:
    print("      - %s" % (p.get("url") or ""))
if len(pages) != 1:
    print("  ✗ 浏览器不干净：期望 1 个空白页，实际 %d 个（上一轮跑崩留下的）" % len(pages)); sys.exit(1)
urls = " ".join((p.get("url") or "") for p in pages)
if ("127.0.0.1:%s" % board) in urls or "127.0.0.1:87" in urls:
    print("  ✗ 浏览器里已经开着一个看板页面 —— 残留标签页会让体检假红"); sys.exit(1)
print("  ✓ 浏览器干净")
PY
# 只在「这一段自己失败」时中止 —— 闸门一带过来的 FAILED 不能算到浏览器头上，
# 不然报错文案（「浏览器不干净」）会把人引去查错误的方向
[ "$BROWSER_DIRTY" = "1" ] && die "浏览器不干净，不往下跑（换一次性浏览器实例即可）"

# 8) 体检
say "看板体检（board-check.js）"
BASE="http://127.0.0.1:${BOARD_PORT}" BROWSER_URL="http://127.0.0.1:${CDP_PORT}" \
  PUPPETEER_DIR="${PUPPETEER_DIR:-$HOME/.workbuddy/binaries/node/workspace/node_modules}" \
  node tools/board-check.js > "$TMP/check.log" 2>&1
CHECK_RC=$?
tail -3 "$TMP/check.log" | sed 's/^/      /'

# 9) 失败项分拣：任何 ✗ 都是新回归（已知假红名单已随演示数据补齐而删除）
say "失败项分拣"
NEW_FAILS=0
if [ "$CHECK_RC" != "0" ]; then
  # board-check.js 会把失败项打印两遍（跑的过程里一次、末尾汇总一次），先去重
  grep '^  ✗ ' "$TMP/check.log" | sed 's/^  ✗ //' | sed 's/  ← .*$//' | sort -u > "$TMP/fails.txt"
  if [ ! -s "$TMP/fails.txt" ]; then
    # 退出码非 0 却一条 ✗ 都没解析出来 = **体检是崩掉的**（detached Frame / 起不了页），
    # 不是「用例失败」。这必须算失败 —— 不然崩一轮反而显示「两道闸门都通过」，
    # 门禁自己给自己放水（2026-10-08 实测踩到过一次，被门禁放行的就是崩的那轮）。
    bad "体检跑崩了（退出码 ${CHECK_RC}，但没有可分拣的失败项）"
    tail -12 "$TMP/check.log" | sed 's/^/      /'
    FAILED=1
  else
    while IFS= read -r name; do
      [ -z "$name" ] && continue
      printf '    新回归：%s\n' "$name"
      NEW_FAILS=$((NEW_FAILS + 1))
    done < "$TMP/fails.txt"
  fi
fi

if [ "$CHECK_RC" != "0" ] && [ "$NEW_FAILS" != "0" ]; then FAILED=1; fi

printf '\n########## %s ##########\n\n' \
  "$([ "$FAILED" = "0" ] && echo '两道闸门都通过' || echo '有失败项，不能放行')"
exit "$FAILED"
