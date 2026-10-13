#!/bin/bash
# 秋招投递进度看板（applytracker）—— 设置开机自启（Mac）
#
# 双击本文件跑一次就行。以后开机登录后，看板会自己在这台电脑的后台跑起来，
# 不用再手动敲命令，也不会弹黑窗口。
#
# 想临时取消：双击同目录的「关闭开机自启.command」
# 注意：看板只监听本机 127.0.0.1，不会暴露到局域网。端口默认 8788，
#       被别的程序占了会自动往上试到 8791（所以开机后它可能不在 8788，
#       本脚本下面的健康检查会把 8788–8791 都看一遍，用实际找到的那个端口）。

cd "$(dirname "$0")" || exit 1
DIR="$(pwd)"
LABEL="com.applytracker.server"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"
UID_NUM="$(id -u)"
PORT_FIRST=8788       # 默认端口
PORT_LAST=8791        # 被占时自动往上试，挑到这儿为止
PORT="$PORT_FIRST"

printf '\n  ========================================\n'
printf '    秋招投递进度看板 —— 设置开机自启\n'
printf '  ========================================\n\n'

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

# 找在跑的看板端口：8788–8791 依次问一遍，返回实际在用的那个（找不到返回 1）。
# 判据收紧为「app == applytracker 且 ok == true」—— 只看 ok 会把别的服务误认成看板。
board_port() {
  local p resp
  p="$PORT_FIRST"
  while [ "$p" -le "$PORT_LAST" ]; do
    resp="$(curl -s -m 3 "http://127.0.0.1:${p}/api/health" 2>/dev/null)" || { p=$((p + 1)); continue; }
    if [ -n "${resp}" ] \
       && printf '%s' "${resp}" | grep -Eq '"app"[[:space:]]*:[[:space:]]*"applytracker"' \
       && printf '%s' "${resp}" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
      printf '%s' "${p}"
      return 0
    fi
    p=$((p + 1))
  done
  return 1
}

# ---------- 1. 确认位置对 ----------
for f in "$DIR"/*.command; do
  [ -f "$f" ] && chmod +x "$f" 2>/dev/null
done

if [ ! -f "$DIR/applytracker.py" ]; then
  printf '  [错误] 这个脚本要跟 applytracker.py 放在同一个文件夹里。\n'
  printf '         当前文件夹：%s\n\n' "$DIR"
  printf '  按回车键关闭...'; read -r _; exit 1
fi

# ---------- 2. 找 python3 ----------
PY_BIN=""
for p in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  [ -x "$p" ] && PY_BIN="$p" && break
done
if [ -z "$PY_BIN" ]; then
  for p in "$HOME"/.workbuddy/binaries/python/versions/*/bin/python3; do
    [ -x "$p" ] && PY_BIN="$p"
  done
fi
if [ -z "$PY_BIN" ]; then
  printf '  [错误] 没找到 python3。macOS 一般自带，先在终端跑一下\n'
  printf '         xcode-select --install 再重试。\n\n'
  printf '  按回车键关闭...'; read -r _; exit 1
fi
printf '  python:    %s  (%s)\n' "$PY_BIN" "$("$PY_BIN" --version 2>&1)"

# ---------- 3. 找 node（lark-cli 要用，服务里读飞书全靠它） ----------
NODE_BIN=""
for p in "$HOME"/.workbuddy/binaries/node/versions/*/bin/node; do
  [ -x "$p" ] && NODE_BIN="$p"
done
if [ -z "$NODE_BIN" ]; then
  for p in /opt/homebrew/bin/node /usr/local/bin/node; do
    [ -x "$p" ] && NODE_BIN="$p" && break
  done
fi
if [ -z "$NODE_BIN" ]; then
  printf '  [警告] 没找到 node —— 看板能打开，但拉不到飞书数据。\n'
  printf '         装好 WorkBuddy（Mac 版）再重跑本脚本。\n'
  NODE_DIR="/usr/local/bin"
else
  NODE_DIR="$(dirname "$NODE_BIN")"
  printf '  node:      %s\n' "$NODE_BIN"
fi

# ---------- 4. 检查自检能不能过（不通过就别装了） ----------
printf '\n  先跑一遍自检...\n'
if ! "$PY_BIN" "$DIR/applytracker.py" --self-test >/tmp/at-selftest.log 2>&1; then
  printf '  [错误] 自检没通过，先别装自启。把下面几行发我：\n\n'
  tail -12 /tmp/at-selftest.log | sed 's/^/    /'
  printf '\n  按回车键关闭...'; read -r _; exit 1
fi
printf '  ✓ 自检通过（%s 项）\n' "$(grep -c '✓' /tmp/at-selftest.log)"

# ---------- 5. 端口上的旧进程让位 ----------
# 分两种：占端口的是「看板自己」（手动起的那个）→ 直接接管，不用问；
#         是别的程序 → 一定要问，别把人家正在跑的东西打掉。
OLD_PIDS="$(lsof -ti :$PORT 2>/dev/null)"
if [ -n "$OLD_PIDS" ]; then
  OURS_ONLY=1
  for pid in $OLD_PIDS; do
    # 两种口径任一命中就算「是看板自己」：pgrep 按命令行匹配，ps 兜底。
    # 两种都拿不到信息时按「不是自己」处理 → 走询问分支，宁可多问一句也不误杀。
    if pgrep -f "applytracker\.py" 2>/dev/null | grep -qx "$pid"; then
      continue
    fi
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "applytracker.py"; then
      continue
    fi
    OURS_ONLY=0
  done

  if [ "$OURS_ONLY" = "1" ]; then
    printf '\n  看板已在运行（手动启动的），交给系统托管 —— 先让旧的让位。\n'
    echo "$OLD_PIDS" | xargs kill 2>/dev/null
    sleep 2
    printf '  已接管。\n'
  else
    printf '\n  端口 %s 被别的程序占着：\n' "$PORT"
    for pid in $OLD_PIDS; do
      printf '    pid %-6s %s\n' "$pid" "$(ps -p "$pid" -o command= 2>/dev/null | cut -c1-70)"
    done
    printf '\n  按 y 再回车 → 关掉它，继续装（关掉后看板就能用 %s 这个默认端口）\n' "$PORT"
    printf '  直接回车   → 不动它，继续装（看板会自己换到 %s–%s 里的空端口）\n' "$((PORT + 1))" "$PORT_LAST"
    printf '  > '; read -r ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
      echo "$OLD_PIDS" | xargs kill 2>/dev/null
      sleep 2
      printf '  已关掉。\n'
    else
      printf '  没动它，继续装（看板等下会换端口跑）。\n\n'
      printf '  按回车键关闭...'; read -r _; exit 0
    fi
  fi
fi

# ---------- 6. 写 plist ----------
mkdir -p "$PLIST_DIR" "$LOG_DIR"

PY_ESC="$(xml_escape "$PY_BIN")"
DIR_ESC="$(xml_escape "$DIR")"
NODEDIR_ESC="$(xml_escape "$NODE_DIR")"
LOG_ESC="$(xml_escape "$LOG_DIR")"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PY_ESC}</string>
        <string>applytracker.py</string>
        <string>--serve</string>
        <string>--no-open</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${DIR_ESC}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${NODEDIR_ESC}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>${LOG_ESC}/applytracker.out.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_ESC}/applytracker.err.log</string>
</dict>
</plist>
PLIST_EOF

printf '\n  配置已写入：%s\n' "$PLIST"

# ---------- 7. 加载（bootstrap 失败就退回旧接口） ----------
launchctl bootout "gui/$UID_NUM" "$PLIST" >/dev/null 2>&1
if ! launchctl bootstrap "gui/$UID_NUM" "$PLIST" >/dev/null 2>&1; then
  launchctl unload "$PLIST" >/dev/null 2>&1
  launchctl load -w "$PLIST" >/dev/null 2>&1
fi

printf '  启动中，稍等一下...\n\n'
sleep 5

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  LOADED="已登记"
else
  LOADED="没登记上"
fi

BOARD_PORT="$(board_port)"
if [ -n "${BOARD_PORT}" ]; then
  printf '\n  --------------------------------------------\n'
  printf '   设置完成，看板已在后台运行。\n\n'
  printf '   打开看板：http://127.0.0.1:%s\n' "${BOARD_PORT}"
  printf '   实际端口：%s（默认 8788；被占时看板会自动往上挑，这里是它真正用上的那个）\n' "${BOARD_PORT}"
  printf '   以后开机登录后它自己就起来了。\n\n'
  printf '   想临时关掉：双击「关闭开机自启.command」\n'
  printf '   想看日志：  %s/applytracker.out.log\n' "$LOG_DIR"
  printf '  --------------------------------------------\n\n'
else
  printf '\n  --------------------------------------------\n'
  printf '   配置写好了（系统那边：%s），但健康检查没通过。\n' "$LOADED"
  printf '   把下面这个文件最后几行发我：\n'
  printf '     %s/applytracker.err.log\n' "$LOG_DIR"
  printf '\n   常见原因：%s–%s 这四个端口都被别的程序占着。\n' "$PORT_FIRST" "$PORT_LAST"
  printf '  --------------------------------------------\n\n'
fi

printf '  按回车键关闭本窗口...'; read -r _
