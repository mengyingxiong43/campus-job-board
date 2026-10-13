# AGENTS.md

`applytracker` 是一个**单文件** Python 看板：把浏览器扩展记下的投递记录、邮箱来的面试 / 测评通知、别人整理好的岗位汇总表，集中到本机一张页面上看。**零第三方依赖（只用 Python 标准库），Python 3.9+**；数据全在使用者本机，服务只监听 `127.0.0.1`。

功能、配置、全部命令的说明在 [`README.md`](README.md)。这里只写三件事：怎么跑起来、什么绝对不能碰、改完跑什么。

## 跑起来

```bash
python3 applytracker.py --serve        # 默认 http://127.0.0.1:8788
```

浏览器打开它打印出来的地址。`config.ini` 是**可选**的：没有就用内置默认值；想改端口 / 数据源，先 `cp config.example.ini config.ini`。macOS / Windows 也可以双击 `start.command` / `start.bat`（同一件事，外加找 Python、挑空闲端口、报错说人话）。

**完成判据**：`curl -s http://127.0.0.1:8788/api/health` 返回 `{"ok": true, "app": "applytracker", ...}`，且 `http://127.0.0.1:8788/` 能打开看板页。

## 使用者的数据 —— 只读

- `data/` 是使用者的**真实记录**（投递 CSV、邮箱通知、各种状态 json）。读它、别写它：不删、不清空、不为了让测试通过而改。
- `config.ini` 里可能有**邮箱 IMAP 授权码**（密码级凭据）—— 不提交、不打印、不写进日志或 commit 信息。
- 两者都被 `.gitignore` 忽略。跑任何会写盘的动作，用 `--base` / `--config` 指到临时目录，别落在仓库里。

## 改完代码跑闸门

```bash
bash tools/ci-gate.sh                 # 两道闸门：自检 + 启动脚本语法 + py_compile + 看板体检
SKIP_BROWSER=1 bash tools/ci-gate.sh  # 没浏览器的机器上只跑第一道
```

**完成判据**：退出码 0。脚本顶部注释列了它到底查哪些东西；看板体检（`tools/board-check.js`）要 `puppeteer-core`，用 `PUPPETEER_DIR` 指到装它的 `node_modules`，或者 `npm i puppeteer-core`。

只想快跑一轮纯 Python 自检：`python3 applytracker.py --self-test` —— 末尾必须打印「✓ 自检全部通过」。断言数量只能增、不能减。

## 可选能力（不配也能跑）

| 能力 | 怎么开 |
| --- | --- |
| 邮箱直连（IMAP） | `config.ini` 的 `[mail]` 填 `user` + 授权码（**不是登录密码**），`[notices] source = imap`；或看板「邮箱通知」页点「连接我的邮箱」 |
| 飞书多维表格同步 | `[notices]` / `[applications]` / `[opportunities]` 填 `feishu_base_token` + `feishu_table_id`；本机需装好并授权 `lark-cli` |
| 浏览器扩展 | Chrome / Edge → `chrome://extensions` → 打开「开发者模式」→「加载已解压的扩展程序」→ 选 `extension/` |

## 容易踩的坑

- **端口**：默认 8788，被占就自动在 8788–8791 里往上找一个；`--port` 则是**只试你指定的那一个**，被占直接报错。
- **改了代码要重起服务**：`--serve` 是前台进程，停掉再起。macOS 装了开机自启的是 launchd 管的：`launchctl kickstart -k gui/$UID/com.applytracker.server`。
- **别拿真实数据跑演示**：`tools/make-demo-data.py` 造脱敏数据，起服务时 `--base` 和 `--config` **两个都给**（`--base /tmp/at-demo --config /tmp/at-demo/config.ini`）。只给 `--base` 会退回仓库里那份带真实 token 的 `config.ini`，把真实记录同步进演示目录 —— README 里记了这条踩过的坑。
- **`samples/` 里全是编造的演示数据**（公司名 / 岗位 / 邮件主题），别把真实企业名或真人信息写进去。
