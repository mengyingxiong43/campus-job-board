#!/bin/bash
# 双击就能打开本地看板（macOS）
#
# 已经在后台跑着 → 直接给你打开浏览器，不会再起一个
# 没在跑 → 起一个，并且这次关掉窗口就是停掉
#
# 下面这段「找 Python、查版本、挑端口」是给没装过 Python 的同学准备的：
# 出问题不闪退，用中文说清是哪一类、下一步做什么。
#
# 注意：变量一律写成 ${VAR}，别写 $VAR —— $VAR 后面紧跟中文/全角标点时，
# bash 会把多字节字符的前几个字节并进变量名，导致取值出错。

cd "$(dirname "$0")" || exit 1

BASE=8788          # 默认端口（也是浏览器扩展默认对齐的端口）
LAST=8791          # 端口挑到这儿为止

# 停住窗口，别让提示一闪就没了
hold_window() {
  echo
  read -r -p "按回车关闭这个窗口…" _
}

# ---------- 0. 已经在跑？直接开浏览器 ----------
# 端口可能被上一次启动挑到了 8789/8790/8791，所以四个都看一眼。
# 判据收紧为「app == applytracker 且 ok == true」—— 只看 ok 会把别的服务误认成看板
# （与「开机自启.command」「关闭开机自启.command」里那两处口径一致）。
if command -v curl >/dev/null 2>&1; then
  p="${BASE}"
  while [ "${p}" -le "${LAST}" ]; do
    resp="$(curl -s -m 1 "http://127.0.0.1:${p}/api/health" 2>/dev/null)"
    if [ -n "${resp}" ] \
       && printf '%s' "${resp}" | grep -Eq '"app"[[:space:]]*:[[:space:]]*"applytracker"' \
       && printf '%s' "${resp}" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'; then
      echo "看板已经在后台跑着了（127.0.0.1:${p}），直接给你打开浏览器。"
      open "http://127.0.0.1:${p}"
      exit 0
    fi
    p=$((p + 1))
  done
fi

# ---------- 1. 找 Python（多路径查找，复用「开机自启.command」里的循环）----------
find_python() {
  # 先按常规位置找 python3
  for p in "$(command -v python3 2>/dev/null)" \
           /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if [ -n "${p}" ] && [ -x "${p}" ]; then printf '%s' "${p}"; return 0; fi
  done
  # WorkBuddy 自带的那套
  for p in "${HOME}"/.workbuddy/binaries/python/versions/*/bin/python3; do
    if [ -x "${p}" ]; then printf '%s' "${p}"; return 0; fi
  done
  # 退一步：只有 python 没有 python3 的机器
  p="$(command -v python 2>/dev/null)"
  if [ -n "${p}" ] && [ -x "${p}" ]; then printf '%s' "${p}"; return 0; fi
  return 1
}

PY="$(find_python)"
if [ -z "${PY}" ]; then
  echo "【没法开始】这台电脑上没找到 Python（需要 3.9 或更新的版本）。"
  echo
  echo "  怎么装："
  echo "    1. 打开 https://www.python.org/downloads/"
  echo "    2. 下载最新的 Python 3 安装包，一路「继续 / 下一步」装完"
  echo "    3. 回到这个文件夹，再双击一次 start.command"
  echo
  echo "  这个黑窗口不要关（关了就看不清上面写了什么）。"
  hold_window
  exit 1
fi

# ---------- 2. 版本校验（要 3.9 或更新）----------
if ! "${PY}" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" 2>/dev/null; then
  VER="$("${PY}" --version 2>&1)"
  echo "【版本太低】找到的 Python 是 ${VER}，这个看板要 3.9 或更新的版本。"
  echo
  echo "  常见两种："
  echo "    - macOS 系统自带的 python3 版本偏低 → 去 https://www.python.org/downloads/ 装一个新的"
  echo "    - 装过但 PATH 里还是旧的那个 → 装完后回到这个文件夹，重新双击一次 start.command"
  echo
  echo "  这个黑窗口不要关（关了就看不清上面写了什么）。"
  hold_window
  exit 1
fi

echo "Python: ${PY}（$("${PY}" --version 2>&1)）"

# ---------- 3. 挑端口（8788–8791 第一个空闲的；有人占就自动让开）----------
# 探测用 300ms 短超时，连得上=被占，连不上=空闲。四个都占才报错。
port_free() {
  "${PY}" - "$1" <<'PYEOF'
import socket, sys
s = socket.socket()
s.settimeout(0.3)
r = s.connect_ex(("127.0.0.1", int(sys.argv[1])))
s.close()
sys.exit(0 if r != 0 else 1)   # 连不上 → 空闲 → 退出码 0
PYEOF
}

PORT=""
p="${BASE}"
while [ "${p}" -le "${LAST}" ]; do
  if port_free "${p}"; then PORT="${p}"; break; fi
  p=$((p + 1))
done

if [ -z "${PORT}" ]; then
  echo "【端口都占着】8788–8791 这四个端口都被别的程序占了。"
  echo
  echo "  先关掉占用它们的程序，再双击一次 start.command。"
  echo "  （想自己指定也行：在本文件夹的终端里跑 python3 applytracker.py --serve --port 9000）"
  echo
  echo "  这个黑窗口不要关（关了就看不清上面写了什么）。"
  hold_window
  exit 1
fi

if [ "${PORT}" != "${BASE}" ]; then
  echo
  echo "  【提示】默认端口 ${BASE} 被占，服务改跑在 ${PORT}，浏览器扩展会自动找到它。"
  echo "          看板地址：http://127.0.0.1:${PORT}"
  echo
fi

# ---------- 4. 起服务 ----------
echo
echo "  服务正在运行，这个黑窗口不要关（关了服务就停了）。"
echo "  看板地址：http://127.0.0.1:${PORT}"
echo

"${PY}" applytracker.py --serve --port "${PORT}"

read -r -p "服务已停止，按回车关闭…" _
