#!/bin/bash
# 秋招投递进度看板（applytracker）—— 关闭开机自启（Mac）
#
# 双击本文件，看板就不再跟着开机自己起来了。
# 想再开：双击同目录的「开机自启.command」。
#
# 这个脚本只动看板自己的自启配置，不碰别的东西。

cd "$(dirname "$0")" || exit 1
LABEL="com.applytracker.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"
PORT_FIRST=8788       # 看板默认端口
PORT_LAST=8791        # 被占时会自动往上试，挑到这儿为止

printf '\n  ========================================\n'
printf '    秋招投递进度看板 —— 关闭开机自启\n'
printf '  ========================================\n\n'

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

if [ ! -f "$PLIST" ]; then
  printf '  本来就没装自启（找不到 %s）。\n' "$PLIST"
  printf '  如果看板还在跑，那是手动起的，关掉终端或重启电脑就没了。\n\n'
  printf '  按回车键关闭...'; read -r _; exit 0
fi

launchctl bootout "gui/$UID_NUM" "$PLIST" >/dev/null 2>&1 || launchctl unload "$PLIST" >/dev/null 2>&1
sleep 2

if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  printf '  [警告] 没关干净，系统那边还登记着。稍后重试一次，或重启电脑。\n\n'
else
  printf '  ✓ 已停止，并且以后开机不会再自己起来了。\n\n'
fi

printf '  要不要顺手删掉那份配置文件？\n'
printf '  （删了也不影响，以后双击「开机自启.command」会重新写一份）\n\n'
printf '  按 y 再回车 → 删掉\n'
printf '  直接回车   → 留着\n'
printf '  > '; read -r ans
if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
  rm -f "$PLIST"
  printf '  已删掉。\n\n'
else
  printf '  留着没动。\n\n'
fi

BOARD_PORT="$(board_port)"
if [ -n "${BOARD_PORT}" ]; then
  printf '  注意：看板进程还在运行（端口 %s，可能是别的窗口起的）。\n' "${BOARD_PORT}"
  printf '  想让端口也空出来，就把那个窗口关掉，或者重启电脑。\n\n'
fi

printf '  按回车键关闭本窗口...'; read -r _
