@echo off
chcp 65001 >nul
REM ===================================================================
REM  applytracker 启动脚本（Windows）
REM
REM  已经在跑 -> 直接开浏览器；没在跑 -> 起一个，关掉这个窗口就停掉。
REM
REM  下面这段「找 Python、查版本、挑端口」是给没装过 Python 的同学准备的：
REM  出问题不闪退，用中文说清是哪一类、下一步做什么。
REM
REM  本文件是 UTF-8，靠开头的 chcp 65001 让中文在窗口里正常显示。
REM ===================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "BASE=8788"
set "LAST=8791"

REM ---------- 0. 已经在跑？直接开浏览器 ----------
where curl >nul 2>nul
if errorlevel 1 goto :skip_running
for /L %%P in (%BASE%,1,%LAST%) do (
  call :is_running %%P
  if "!RUNNING!"=="1" (
    echo 看板已经在后台跑着了（127.0.0.1:%%P），直接给你打开浏览器。
    start "" "http://127.0.0.1:%%P"
    exit /b 0
  )
)
:skip_running

REM ---------- 1. 找 Python（python3 -> python -> py -3，取第一个能用的）----------
REM 「能用」= 能跑起来并报出版本；顺带把「装没装」记下来，好分开给提示。
set "FOUND_ANY="
python3 --version >nul 2>nul
if not errorlevel 1 set "FOUND_ANY=1"
python --version >nul 2>nul
if not errorlevel 1 set "FOUND_ANY=1"
py -3 --version >nul 2>nul
if not errorlevel 1 set "FOUND_ANY=1"

set "PY="
python3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python3"
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto :pick_failed

echo Python: %PY%

REM ---------- 2. 挑端口（8788-8791 第一个空闲的）----------
set "PORT="
for /L %%P in (%BASE%,1,%LAST%) do (
  if not defined PORT (
    call :port_free %%P
    if "!FREE!"=="1" set "PORT=%%P"
  )
)
if not defined PORT goto :all_busy

if not "%PORT%"=="%BASE%" (
  echo.
  echo   【提示】默认端口 %BASE% 被占，服务改跑在 %PORT%，浏览器扩展会自动找到它。
  echo           看板地址：http://127.0.0.1:%PORT%
  echo.
)

REM ---------- 3. 起服务 ----------
echo.
echo   服务正在运行，这个黑窗口不要关（关了服务就停了）。
echo   看板地址：http://127.0.0.1:%PORT%
echo

%PY% applytracker.py --serve --port %PORT%

echo.
echo 服务已停止。
pause
exit /b 0

REM ================= 子程序 =================

:is_running
set "RUNNING=0"
curl -s -m 1 "http://127.0.0.1:%1/api/health" 2>nul | findstr /C:"applytracker" >nul 2>nul
if not errorlevel 1 set "RUNNING=1"
exit /b

:port_free
set "FREE=0"
REM python 探测：connect_ex 连不上 -> 空闲（退出码 0）
%PY% -c "import socket,sys;s=socket.socket();s.settimeout(0.3);sys.exit(0 if s.connect_ex(('127.0.0.1',int(sys.argv[1])))!=0 else 1)" %1 >nul 2>nul
if not errorlevel 1 set "FREE=1"
exit /b

:pick_failed
if defined FOUND_ANY goto :bad_version
goto :no_python

:no_python
echo 【没法开始】这台电脑上没找到 Python（需要 3.9 或更新的版本）。
echo.
echo   怎么装：
echo     1. 打开 https://www.python.org/downloads/
echo     2. 下载最新的 Python 3 安装包
echo     3. 安装时务必勾上下面的 "Add Python to PATH"（Add Python 3.x to PATH）
echo     4. 装完回到这个文件夹，双击 start.bat
echo.
echo   如果已经装过还是提示没找到：多半是安装时没勾上面的 PATH 选项。
echo   重新运行一次安装包，选 Modify / Repair，把 "Add Python to PATH" 勾上。
echo.
echo   这个黑窗口不要关（关了就看不清上面写了什么）。
echo.
pause
exit /b 1

:bad_version
set "VER=(未知)"
for /f "delims=" %%V in ('python3 --version 2^>^&1') do set "VER=%%V"
if "%VER%"=="(未知)" for /f "delims=" %%V in ('python --version 2^>^&1') do set "VER=%%V"
if "%VER%"=="(未知)" for /f "delims=" %%V in ('py -3 --version 2^>^&1') do set "VER=%%V"
echo 【版本太低】找到的 Python 是 %VER%，这个看板要 3.9 或更新的版本。
echo.
echo   去 https://www.python.org/downloads/ 装一个 3.9 以上的新版本，
echo   装完后回到这个文件夹，再双击一次 start.bat。
echo.
echo   这个黑窗口不要关（关了就看不清上面写了什么）。
echo.
pause
exit /b 1

:all_busy
echo 【端口都占着】8788-8791 这四个端口都被别的程序占了。
echo.
echo   先关掉占用它们的程序，再双击一次 start.bat。
echo   （想自己指定也行：在本文件夹打开终端，跑 python applytracker.py --serve --port 9000）
echo.
echo   这个黑窗口不要关（关了就看不清上面写了什么）。
echo.
pause
exit /b 1
