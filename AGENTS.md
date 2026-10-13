# AGENTS.md

`applytracker` 是一个**单文件** Python 看板：把浏览器扩展记下的投递记录、邮箱来的面试 / 测评通知、别人整理好的岗位汇总表，集中到本机一张页面上看。**零第三方依赖（只用 Python 标准库），Python 3.9+**；数据全在使用者本机，服务只监听 `127.0.0.1`。

功能、配置、全部命令的说明在 [`README.md`](README.md)。这里只写四件事：怎么跑起来、**跑起来之后要带使用者做完哪几步**、什么绝对不能碰、改完跑什么。

## 跑起来

```bash
python3 applytracker.py --serve        # 默认 http://127.0.0.1:8788
```

浏览器打开它打印出来的地址。`config.ini` 是**可选**的：没有就用内置默认值；想改端口 / 数据源，先 `cp config.example.ini config.ini`。macOS / Windows 也可以双击 `start.command` / `start.bat`（同一件事，外加找 Python、挑空闲端口、报错说人话）。

**完成判据**：`curl -s http://127.0.0.1:8788/api/health` 返回 `{"ok": true, "app": "applytracker", ...}`，且 `http://127.0.0.1:8788/` 能打开看板页。

## 跑起来之后：主动带使用者做完这三步

**服务起来不等于装完** —— 一个只有空看板的人拿不到这套东西的任何好处。别等他问，按顺序做完：

**① 确认机会库真的有数据。** `curl -s 'http://127.0.0.1:8788/api/opps?limit=1'` 里的 `total_rows` 应该是 **9381**（随包快照 `snapshot/opportunities.json` 首次启动自动预置进 `data/`，不用配任何东西）。是 0 就去看启动日志里有没有「机会库首次预置完成」。

**② 带他装浏览器扩展**（自动记投递记录靠它）：

- 你可以替他打开 `edge://extensions`（Chrome 是 `chrome://extensions`）、把右上角「开发者模式」打开；
- **但「加载已解压的扩展程序」那一下必须他本人点，文件夹选仓库里的 `extension/`** —— 浏览器不允许程序替人把扩展装进常用浏览器。**别用 `--load-extension` 之类的方式绕过**：那条路只在「这次用这个参数启动的浏览器进程」里生效，他关掉浏览器再打开就没了，属于假成功。把位置指清楚，等他点完，再一起确认扩展列表里出现了「投递记录助手」。
- 装完把用法讲清楚（一句话就够）：**在招聘网站点下「提交申请」会自动弹卡，看一眼点「记下来」；哪次没弹卡，就点页面右边竖着的「记一笔」**。记进去的在「投递进度」页，一家一行。

**③ 带他连邮箱**（面试 / 测评的截止时间靠它自己进来）：

- 打开看板「邮箱通知」页 →「连接我的邮箱」，陪他填「邮箱地址 + 授权码」，点「连接」。服务器和端口会自动认。
- 先告诉他授权码去哪拿：**QQ / Foxmail** 是 `mail.qq.com` → 设置 → 账户 → 开启 IMAP/SMTP 服务 → 短信验证 → 弹窗里那串 16 位码；**163 / 126** 是「客户端授权密码」；**Gmail** 要「应用专用密码」；学校邮箱让他问 IT，弹卡里「服务商」选「其他邮箱」手填服务器。**是授权码，不是邮箱登录密码** —— 这一步最容易填错。
- ⚠️ 授权码是密码级凭据：填完**不要**写进任何文件、日志、commit 信息或聊天记录。`config.ini` 已被 `.gitignore` 忽略，页面写入时会把权限收成 600。
- **完成判据**：「邮箱通知」页出现真实通知（不是空白，也不是报错）。没出现就让他在页面上点一次「收一次邮件」，把页面上的报错原文拿来查（README 第 4 节有排查路径）。

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
