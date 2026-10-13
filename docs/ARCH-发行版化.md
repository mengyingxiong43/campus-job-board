# ARCH-发行版化 · 技术方案

> 版本 v0.1｜作者：高见远（架构师）｜日期：2026-10-07
> 对应 PRD：`docs/PRD-发行版.md`｜配套 ADR：`docs/decisions/ADR-001..007`
> 结论先行：**不改产品形态（单文件、零依赖、内嵌前端、本机一套），只做「换链路 + 三处加固 + 打包 + 引导」。**

---

## 0. 锁定项总览

| # | 决策 | 锁定值 | ADR |
|---|------|--------|-----|
| L1 | 邮箱直连 | 标准库 `imaplib.IMAP4_SSL` + `email` + `ssl`，授权码登录 | ADR-001 |
| L2 | 通知识别 | 纯规则引擎（移植/扩写 `qqmail2table.py`），**不用大模型** | ADR-002 |
| L3 | 邮箱代码归属 | **内联进 `applytracker.py`** 新章节（不拆模块、不引子进程） | 本文件 §2 |
| L4 | 新增数据源 | `[notices] source` 增加 `imap` 值（与 `feishu`/`file`/`auto` 并列） | 本文件 §2 |
| L5 | 打包 | `git archive <tag>` + 打包后私有数据扫描 | ADR-003 |
| L6 | 本地服务信任边界 | 关通配 CORS，改 `_origin_ok()` 校验 | ADR-004 |
| L7 | 写入一致性 | 文件级互斥锁 + 原子写 + 保留自定义列 | ADR-005 |
| L8 | 备份 | 每次启动按日快照 `data/backups/`，留 14 天 | ADR-006 |
| L9 | 父链上限 | 6 → 20，同步改自检断言 | ADR-007 |
| L10 | 图标/配色 | 沿用现有内联 unicode 符号，**不引图标库、不用 emoji 作功能图标、不硬编码颜色（`#fff`/`#000` 除外）** | §7 |
| L11 | 启动脚本 | Python 检测（`python3`→`python`→`py -3`）+ 版本 ≥3.9 校验 + 中文提示并停住 | §5.6 |
| L12 | 环境自检 | `env_selfcheck(cfg)` 一份输出，终端/网页/`--doctor` 三处复用；自检永不阻断启动 | §5.7 |
| L13 | 端口发现 | 服务端两条路都会挑：脚本（`start.command`/`start.bat`）挑 8788–8791 并明确打印；**`serve()` 自己也挑**（`pick_port`，v0.30.0 已落地，覆盖开机自启/直接 `--serve` 这两条绕过脚本的路径）；扩展端自动探测同范围 + `/api/health` 身份校验，全失败才手填 | ADR-008 |

**明确不做**（PRD 4.3 + 硬约束）：不拆多文件工程、不动数据格式、不动已定型界面、不引第三方依赖、
不做账号体系/多租户/云端、v1 不打包 exe/app、Gmail/Outlook OAuth 不进 v1、不做联网埋点。

---

## 1. 技术选型对比（邮箱直连）

约束：零第三方依赖 + Python 3.9。见 ADR-001 完整对比。结论：

| 方案 | 覆盖 | 增量/正文解析 | 零依赖 | 结论 |
|------|------|----------------|--------|------|
| **imaplib 标准库** | QQ/163/学校邮箱 ✓ | ✓ | ✓ | **采用** |
| OAuth/XOAUTH2 | 仅 Gmail/Outlook 必需 | ✓（工程量大） | 需注册应用/令牌刷新 | 留 SEAM，不进 v1 |
| 第三方 imapclient | ✓ | ✓ | **✗** | 排除 |

**可复用资产**：`../qqmail-job-tracker/qqmail2table.py`（1960 行，纯标准库）已实现
imaplib 拉取（`fetch_mails` :556）、正文/链接解析（`extract_body` :276 / `extract_links` :301）、
去重状态（`load_state/save_state/dedupe` :648-681）、配置向导（`run_wizard` :1055）、登录自测（`test_login` :1019）、
以及一整套抽取规则（`TYPE_RULES` :46 / `extract_deadline` :401 / `extract_company` :458）。
**它产出的 CSV 列（`CSV_COLUMNS` :688）与 applytracker 的 `notices.csv` 本就兼容** —— 这是本方案最大的降险点。

---

## 2. 邮箱链路落地要点（L1–L4）

### 2.1 代码归属：内联进单文件
- 在 `applytracker.py` 新增一个章节「邮箱直连（IMAP）」，移植 `qqmail2table.py` 的纯函数：
  `_decode_header_str` / `_decode_payload` / `_strip_html` / `extract_body` / `extract_links` /
  `test_login` / `_imap_utf7_encode` / `load_state` / `save_state` / `dedupe` / `extract_*` / `classify` / `narrow_types`。
- **不引子进程、不拆模块**（受「单文件」硬约束；`qqmail2table.py` 需移植的净代码约 400 行）。
- 配置从 applytracker 的 `config.ini` 读，新增 `[mail]` 段；产物落 `data/notices.csv`，状态落 `data/.mail_state.json`。

### 2.2 新增 `[mail]` 配置段（`config.example.ini` 与 `Config.__init__`）
```ini
[mail]
source = off            ; off | imap
host = imap.qq.com      ; 预设表见 ADR-001（QQ/163/126/Gmail/Outlook/自填学校）
port = 993
user =
authcode =              ; 授权码（非登录密码）；也可用环境变量 APPLYTRACKER_MAIL_AUTHCODE
folder = INBOX
days_back = 30          ; 每次回扫窗口（增量靠 .mail_state 去重）
only_unseen = false
ttl = 900               ; 后台自动拉取节流（秒）
```

### 2.3 数据源接入（`Config.__init__`，`applytracker.py:119-131`）
- `source` 合法值加 `imap`；`auto` 语义扩展为：
  `配了 [mail].user+authcode → imap`，否则 `配了飞书 token → feishu`，否则 `file`（有 CSV）。
- `source == "imap"` 时 `notices_csv = out_dir/notices.csv`（与 feishu 分支同款，`applytracker.py:128`）。
- **`load_notices`（`applytracker.py:2517`）零改动** —— 它只读 CSV，不关心来源。

### 2.4 调度与命令行
- `serve()`（`applytracker.py:6839`）里，`notices_source == "imap"` 时起一个 TTL 后台线程
  （照抄 `feishu` 的 `sync_watchdog`，6847）。
- `--notices-sync`（`applytracker.py:8557`）对 `imap` 也生效：立刻拉一次。
- 新增 `--mail-config`（跑 `run_wizard` 等价流程，收邮箱+授权码，当场 `test_login`）。
- 看板「邮箱通知」页未配置时给出「怎么配」提示，不报错（PRD 旅程第 6 步「允许跳过」）。

---

## 3. 校招通知识别规则（L2，详见 ADR-002）

五类：**资料补充 / AI面 / 笔试 / 测评 / 面试**（顺序即优先级）。相对 `qqmail2table.py:46` 的纠偏：
1. `AI面` 移除 `视频面试`（歧义，会误判）；
2. `笔试` 从 `测评` 独立成类（对齐看板阶梯 `INTERVIEW_ORDER`，`applytracker.py:2600`）；
3. 新增 `资料补充` 类。
4. 否定类（拒信/宣讲会/退订…）→ 写 `data/mail_skipped.jsonl`，**不冒充五类**。

**宁可不标的三道闸**：
- 弱信号（仅正文命中）→ **照样进通知表，类型留空**，页面打「待确认」角标让人点一下选类型
  （2026-10-08 改：早先这类落 `data/mail_review.csv` 一个单独文件，拿到发行版的人根本不会想起来去翻）；
- 多类型同分并列 → 同上，类型留空；
- 截止时间/岗位抽不到 → 留空，**不推断**。

⚠️ **两种失败必须分开，别混**：
- **公司名对不上** → 不知道是哪家的，进 `mail_unmatched.csv`，**不**进列表；
- **类型认不出但公司认得出** → 进通知表，类型留空。

数据里类型就是空串，**「待确认」三个字由前端渲染角标时加** —— UI 文案不写进数据。
反过来手填类型走 `POST /api/notice-type`，直接改写 `notices.csv`（不再落旁路覆盖表，
避免两个地方各说一套）；因为 `notice_state_key` 里含类型，**改类型 key 就变**，
所以挂在旧 key 上的「处理状态」和「识别依据」会一并搬到新 key。

**可复现落地**：命中依据（规则名+命中词+发件人）写旁路 `data/notice_evidence.json`，键用
`notice_state_key`（`applytracker.py:1352`）。**不改 notices.csv 列**（`NOTICE_FIELDS`，`applytracker.py:1342`）。

---

## 4. 公司名匹配策略（复用现有归一）

目标：把邮件里的公司名对到用户投递进度表的公司名，处理「XX银行杭州分行」vs「XX银行」这类差异。
**只在用户自己的名单里匹配**（投递记录 + 待投递 + 机会库），**绝不从邮件正文臆造公司名**。

### 4.1 三级匹配
1. **别名直命中**：`data/mail_aliases.json`（用户归类过的 发件人域名/地址 → 公司名），`extract_company` 的 `alias` 逻辑（`qqmail2table.py:462`）保留。
2. **名字匹配**：复用 `norm_company`（`applytracker.py:1318`，去括号+通用后缀）与 `same_company`（1327，子串）；
   再加一层 `norm_company_core`：**仅在比较时**额外剥地域/机构后缀（省/市/区/分行/支行/分公司/办事处/研究院/事业部…），
   让「XX银行杭州分行」的核心「XX银行」命中。显示名仍取用户名单里的原名。
3. **域名匹配**：发件人域名前缀对已知公司名（弱证据，仅当无 1/2 命中时用，且需域名公司名长度 ≥ 4）。

### 4.2 匹配不上的处理（宁缺毋滥）
- **不进通知表、不挂到任何公司**（避免错标）。
- 写 `data/mail_unmatched.csv`（保留 发件人/域名/主题/收到时间/摘要）。
- 看板「邮箱通知」页顶部出现提示条「N 封没认出公司」，点开可一键指定「这是 XX 公司」→ 写
  `data/mail_aliases.json`（键 = 发件人域名）→ 下次同域自动匹配。**越用越准，且全程无大模型。**
- `known_companies`（`applytracker.py:655`）与 `pick_company_hit`（610）已是现成的「在文本里认自家公司」工具，直接复用。

---

## 5. 分发架构（L5）

### 5.1 打包脚本 `tools/make-release.py`（详见 ADR-003）
门禁（self-test + board-check + `git status` 干净）→ `git archive --prefix=applytracker-{ver}/ <tag>` →
**私有数据扫描**（禁 `data/**` 与 `config.ini`；用当前工作树的真实 token/公司名当词表搜包内文本，命中即中止）→ 通过才落 `dist/`。

### 5.2 机会库快照进包（答复 PM Q3）
`data/opportunities.json`（6.5 MB）被 `.gitignore` 忽略、`git archive` 带不出来。
→ 改为放**被跟踪**的 `snapshot/opportunities.json`，**首次启动时拷贝进 `data/`**（若 `data/` 里已有则不覆盖）。
这样：进得了包、又与用户私有 `data/` 隔离、且用户之后可自行更新。6.5 MB 随包可接受。

### 5.3 首次启动引导
- **`config.ini` 自动生成**：`.gitignore` 已忽略 `config.ini`（仓库第 1 行），不会进包。
  `serve()` 启动时若 `config.ini` 不存在 → 从被跟踪的 `config.example.ini` 复制一份（并把 `chmod 0o600`）。
- **`data/` 自动创建**：`ensure_data_dir(cfg)` 在启动时建 `data/` 与 `data/backups/`；
  `_write_all` 已有 `os.makedirs`（`applytracker.py:220`）兜底。
- **lark-cli 缺失优雅降级**：`_find_lark_cli`（`applytracker.py:1423`）找不到返回空串，代码已抛带人话的 `RuntimeError`（`applytracker.py:894`）。
  发行版默认 **不配飞书** → `source = auto` 根本不会走到 lark-cli。**关键**：见 §5.8 的 `config.example.ini` 默认值修正。

### 5.4 安装说明结构（面向没装过 Python 的同学）
在 `README.md` 顶部新增一节「**给同学的安装步骤**」（PRD 旅程 0–9 落地）：
1. 装 Python 3.9+（macOS 自带可跳过；Windows **必须勾 Add to PATH**，配大字提示）→ 脚本会**自检并给中文提示**（见 §5.6）；
2. 解压（**单层目录**，第一层直接见文件）；
3. 双击 `start.command`（macOS，首次需右键→打开）/ `start.bat`（Windows）→ **窗口停住、逐项自检**（见 §5.6/§5.7）；
4. 浏览器打开 `http://127.0.0.1:8788`（自动弹出）；黑窗口**不要关**；
5. （可选）装扩展：`edge://extensions` → 开发者模式 → 加载解压缩的扩展 → 选 **`extension/` 目录本身**；
6. （可选）连邮箱：填 IMAP 地址 + 授权码（附 QQ/163/Gmail/Outlook 各一页图文）；
7. 数据在哪、怎么备份（`data/applications.csv`，复制走即备份）。

### 5.5 扩展分发包 + 端口（答复 PM Q1，裁决见 ADR-008）
- **分发**：扩展随包（`extension/` 目录），装法 = Edge 开发者模式加载（同 5.4 第 5 步）。
- **端口口径（Q1 答复）**：确认**没有独立的 8787 写入端** —— 扩展写入 = 同一本地服务的 `POST /api/apply`（`applytracker.py:6717`，默认 8788）。
- **服务端（团队裁决，已实现）**：默认 8788；**被占时自动在 8788–8791 内挑第一个空闲端口**，并在黑窗口
  **明确打印**实际端口（如「默认端口 8788 被占，服务改跑在 8790，浏览器扩展会自动找到它」，**不静默**）；四端口全占才报错。
  **脚本端落点 = `start.command`/`start.bat`**（探测用一段 Python 一行 `socket.connect_ex`，0.3s 超时；挑到后 `--serve --port N`）。
- **服务端兜底（v0.30.0 已落地，不再是缺口）**：`serve()` 自己调 `pick_port()`（`applytracker.py:8627`），
  覆盖**所有**启动路径 —— 包括 `开机自启.command` 的 LaunchAgent `--serve` 和用户直接 `python3 applytracker.py --serve`
  （这两条绕过脚本、以前会硬失败）。
  **口径：不区分「是不是显式传了 `--port`」** —— 显式指的、从配置读来的默认值，被占一律往上让。
  少一个分支，两条启动路径自然都对；`pick_port` 的 docstring 里写了这条理由。
  **唯一例外**：`--port` 自己就超出 8788–8791 区间（比如 `--port 9000`）时**只在它身上试一次**，不擅自漂到 9001
  —— 人既然手点了那个端口，就按那个端口办。区间内全占满 → 抛中文 `OSError`，`serve()` 翻成人话并 `return 1`。
- **扩展端（采纳团队裁决，不靠用户手填）**：
  - 扩展连不上当前 `at_port` 时，**自动探测 8788 / 8789 / 8790 / 8791**，命中即写回 `at_port`；
  - **每个候选端口必须通过身份校验**才认：GET `/api/health` 返回 `app == "applytracker"` 且带 `version`
    （`applytracker.py:6700` 已返回 `{"ok":true,"app":"applytracker","version":...}`）；非 200 / 非 JSON / 身份不符 → 跳过该端口；
  - **全部失败才显示「服务端口」输入框**兜底（`popup.html` 新增，写回 `at_port`）；
  - 探测必须在 **background service worker** 里发（其 Origin 是 `chrome-extension://`，过 ADR-004 的 `_origin_ok`）；
    从 content script 发会被 Origin 校验拒。
  - 完整成本/误判评估见 **ADR-008**。
- **净效果**：同学连「遇到端口占用」都不需要处理 —— 服务端自动挪、扩展自动找。
- **`popup.js:3` 仍要改**：现在把看板地址写死成 `http://127.0.0.1:8788/`，「打开看板」按钮不理自定义端口 → 改为读 `at_port` 拼 URL。
- **`manifest.json:7` 无需改**：`host_permissions` 是 host 级（`http://127.0.0.1/*` 不带端口）→ 允许任意端口，探测技术成立。

### 5.6 启动脚本的 Python 检测（缺口一）
**现状核实（逐字读脚本得到，不是推测）：**

| 脚本 | 现状 | 缺口 |
|------|------|------|
| `start.command`（macOS，:18-23） | `PY="$(command -v python3)"`；为空则 `echo "没找到 python3，先装一个再试。"` + `read -r -p "按回车关闭…"` → **有中文提示、有停住** | **无版本校验**（`command -v` 可能返回 <3.9 的 python3）；查找面窄（只看 PATH）；没把「装完怎么验证」写清 |
| `start.bat`（Windows，:1-4） | 只有 `python applytracker.py --serve` + `pause`。**没有任何检测**，且用的是 `python` 不是 `python3` | 没装 Python / 没勾 PATH 时，用户看到的是 cmd 的英文报错（`'python' 不是内部或外部命令`），**不是可行动的中文提示**；无版本校验；`python`/`python3`/`py` 兼容性差 |

> **纠正一处过度描述**：`pause`（bat）与 `read`（command）**已经能防止窗口一闪而过** —— 真正的缺口是「**报错不可读、不告诉下一步**」和「**没有版本校验**」，不是闪退。

**改法（写进脚本，不靠文档兜底）：**
- 两个脚本统一检测顺序：`python3` → `python` → `py -3`，取第一个能跑的；
- 加**版本校验**：`<PY> -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)"`；
- 失败时**中文提示 + 停住**，提示三类常见原因并给出下一步：
  「① 没装 Python → 去 python.org/downloads 装 3.9+；② Windows 装了但没勾 **Add to PATH** → 重新装一次勾上；
   ③ macOS 自带的 python3 版本太低 → 装一个新的」+ `pause`/`read`；
- macOS 侧**复用 `开机自启.command:38-52` 已有的多路径查找循环**（`/usr/bin`、`/usr/local/bin`、`/opt/homebrew/bin`）替换掉只看 PATH 的 `command -v`；
- 两个脚本都保留「已在跑就直接开浏览器」的 `curl /api/health` 前置（`start.command:12-16`；bat 侧用 curl，Win10 1803+ 自带，缺失则跳过）；
- 提示语里带上「这个黑窗口不要关」。

### 5.7 首次启动环境自检（缺口二）
**分层：能靠 shell 判的放 shell，能靠服务判的放服务，用户真正会看的放网页。** 不新增第三方依赖、不加独立 GUI。

| 层 | 位置 | 检查项 | 失败时用户看到 |
|----|------|--------|----------------|
| A 前置 | `start.command`/`start.bat` | Python 是否存在 + 版本 ≥3.9 | 中文提示 + 停住（见 §5.6） |
| B 终端 | `serve()` 启动打印（`applytracker.py:6839`） | 依次打印一行检查清单：Python 版本 / 端口 / `data/` 可写 / `config.ini` / 机会库快照 / 邮箱是否已配 | 每行前 `✓`/`✗` + 一句「怎么修」 |
| C 网页（主界面） | 新增 `GET /api/selfcheck` + 邮箱通知页顶部一个自检面板（复用同一函数输出） | 同上（`data/` 可写、快照、邮箱配置等） | 面板列出「哪一项没过 + 点这里怎么修」，未过的项**不阻塞看板**，可关闭 |
| D 命令行（排障） | `python3 applytracker.py --doctor` | 同 B/C，供出问题时截图给作者 | 同 B |

**实现要点**：
- 抽一个纯函数 `env_selfcheck(cfg) -> List[dict]`（每项 `{name, ok, detail, fix}`），B/C/D 共用同一份输出，避免口径漂移；
- `data/` 可写检查 = 试着在 `data/` 建目录 + 写一个临时文件再删（真写一次，比看权限位靠谱）；
- **自检永不阻断启动**（与备份同款纪律）：任何异常只记一行 warning，继续起服务；
- 端口检查 = `pick_port` 的结论（最终落在哪个端口 / 四端口全占时的报错），自检面板**复用它的结论**而不是重复探测；
- 邮箱「未配置」在自检里是**提示不是错误**（可跳过，PRD 旅程第 6 步）。

### 5.8 `config.example.ini` 默认值锁定（答复 PM Q4）
**必须先修一个现存 bug**：`config.example.ini:67` 的 `[applications] source = feishu` 在没 tokens 时
仍被判为 `feishu`（见 `applytracker.py:150-153`：只有 `auto` 才回退 `off`）→ 新用户每次启动都去同步飞书并失败。
**修正后锁定默认值**：

| 段 | 键 | 锁定值 | 理由 |
|----|----|--------|------|
| `[notices]` | `source` | `auto` | 配了邮箱→imap，配了飞书→feishu，有 CSV→file，都没有→off（**不报错、可跳过**） |
| `[applications]` | `source` | `off` | 本地 CSV 是唯一真相源；**修掉 :67 的 feishu** |
| `[opportunities]` | `source` | `off` | 读随包快照 |
| `[mail]` | `source` | `off` | 未配置时不联网 |

> 为什么不按 PRD 写死 `[notices] source = imap`：imap 需要授权码，未配置就联网会失败。
> `auto` 能达到「配了就 imap」的效果，且对未配置用户给出可跳过的提示，更稳。

---

## 6. 三处加固 + 一个 bug（L6–L9，详见 ADR-004..007）

| 项 | 现状 | 修法 | 影响行号 |
|----|------|------|----------|
| **CORS** | 全响应 `ACAO: *`；OPTIONS 204 直通；POST 只查 Host | 删 `ACAO: *`；新增 `_origin_ok()`，GET/POST/OPTIONS 全过两道校验，不过 → 403 | 6629-6630、6653-6654、6656、6704、6638-6641 |
| **写入丢数据** | 无锁 load→改→`_write_all`，10 并发只进 3 条 | 新增 `APP_FILE_LOCK`(RLock) + `mutate_apps(cfg, fn)`，写入路径全走它；原子写保留 | 219-227、283-337、455、1077/1254/1297 |
| **抹列** | `load_apps`/`_write_all` 只认 `CSV_FIELDS` 十列 | 保留原始列序与自定义列；`--import-apps` 共用同一写盘 | 213、223-226、1044-1045 |
| **每日备份** | data/ 零备份 | 启动时 `daily_backup(cfg)` → `data/backups/`，留 14 天；失败不阻断 | serve() 6839 |
| **父链 6 跳** | 9 层链上溯断在第 6 跳 | `6`→`20`，**同步改自检断言 8176-8178** | 4223-4237、8176-8178 |
| **启动脚本** | `start.command` 无版本校验；`start.bat` 无检测、用 `python` | 两脚本加 `python3→python→py -3` + 版本校验 + 中文提示停住 | start.command:18-23、start.bat:1-4 |
| **端口冲突** | 已有：bind 失败打中文提示并 `return 1` | **已实现（两条路都覆盖）**：start 脚本在 8788–8791 挑空闲端口并明确打印（探测用 Python 一行）；**`serve()` 内 `pick_port()` 兜底**（v0.30.0 落地，绕过脚本的开机自启/直接 `--serve` 也不再硬失败）；四端口全占才报中文错。扩展端自动探测（ADR-008） | start.command/start.bat 全段、applytracker.py:8577-8601 + 8626-8635、background.js:5-15、popup.js:3 |

---

## 7. 图标与配色约束（P0，L10）
- 现有看板/扩展**未使用任何 `<svg>`、也未用 emoji 作功能图标**（实测 `grep "<svg"` = 0；
  现用内联 unicode 符号如 `→ ✓ ×`）。发行版**沿用现状**。
- 决策：**不引任何图标库**（引 JS 图标库违反零依赖）；如新 UI 需要图标，用**内联 SVG sprite**（写进 `HTML_TEMPLATE`，零依赖）。
- 不硬编码颜色（`#fff`/`#000` 除外）——新增样式一律走现有 CSS 变量。
- 文案：不写 AI 模板味空话（ADR/README/引导页均按作者口语风格）。

---

## 8. 影响文件与行号汇总

| 文件 | 改动 | 关键行号 |
|------|------|----------|
| `applytracker.py` | CORS/Origin 校验 | 6623-6633、6638-6641、6653-6654、6656、6704-6707 |
| `applytracker.py` | 写入锁+原子+保列 | 203-227、283-337、455、1044-1045、1077 |
| `applytracker.py` | 备份 | serve() 6839 附近新增 `daily_backup` |
| `applytracker.py` | 父链 6→20 | 4223-4237（改）、**8176-8178（自检断言必须同步改）** |
| `applytracker.py` | 邮箱章节（新） | 新增 IMAP 章节；`Config` 119-131 加 `imap`；`serve()` 加 watchdog；CLI 8540-8564 |
| `applytracker.py` | 环境自检（新） | `env_selfcheck()` + `GET /api/selfcheck` + `--doctor`；`serve()` 启动打印；端口结论复用 `pick_port` 的结论 |
| `applytracker.py` | 端口兜底（v0.30.0 已落地） | `pick_port()` 8577-8601 + `serve()` 8626-8635；不区分显式 `--port`，被占一律在 8788–8791 内往上让 | 8577-8601、8626-8635 |
| `config.example.ini` | 默认值修正 | **67（source=feishu→off）**、25、79；新增 `[mail]` 段 |
| `start.command` | 检测+挑端口 | 已重写：多路径找 Python + 版本 ≥3.9 校验 + 中文提示停住 + 8788–8791 挑空闲端口 + 明确打印（全段） |
| `start.bat` | 检测+挑端口 | 已重写：`python3→python→py -3` + 版本校验 + 中文提示 + 8788–8791 挑空闲端口 + `pause`（全段，UTF-8 + chcp 65001） |
| `extension/popup.js` | 端口可配 | 3（读 `at_port`） |
| `extension/popup.html` | 端口输入框 | 新增 |
| `extension/background.js` | 端口自动探测 | 5-15（新增 8788-8791 探测 + `/api/health` 身份校验 + 写回 `at_port`） |
| `extension/manifest.json` | 无需改 | 7（host 级，端口无关） |
| `.gitignore` | 加 `data/backups/` | — |
| `tools/make-release.py` | 新增 | — |
| `snapshot/opportunities.json` | 新增（被跟踪） | — |
| `README.md` | 顶部加安装步骤；核对父链描述 | — |
| `docs/decisions/ADR-001..008` | 新增 | — |
| `docs/decisions/ADR-001..007` | 新增 | — |

---

## 9. 风险与对策

| 风险 | 等级 | 对策 |
|------|------|------|
| 端口被占 / 扩展对不上服务 | 低 | start 脚本自动挑 8788-8791 并明确打印；**`serve()` 内 `pick_port()` 兜底，开机自启/直接 `--serve` 同样会挪**（v0.30.0 落地，此前的残余风险已关闭）；扩展端自动探测 + `/api/health` 身份校验（ADR-008）；`popup` 显示已连端口 |
| 扩展探测命中「另一个 applytracker 实例」 | 低 | 只读探测；取最低端口优先；`popup` 显示绑定端口；文档写「别同时开两个实例」（ADR-008） |
| 163 需先发 `ID` 命令否则 `Unsafe Login` | 中 | 登录后 `try: xatom("ID", ...) except: pass`（ADR-001） |
| Gmail/Outlook 授权码不可用 | 中 | v1 只走应用密码降级；不进默认引导；OAuth 留 SEAM |
| 规则漏掉新模板 | 中 | 待确认区 + `mail_aliases` 一次归类；绝不硬猜 |
| 误删/覆盖致数据丢失 | 低 | ADR-005 锁 + ADR-006 每日备份双保险 |
| 打包误带私有数据 | 高（一次即灾难） | ADR-003 强制扫描，用工作树真实值当词表，命中即中止 |
| 改 6→20 忘改自检断言 → 自检变红 | 高 | 列进影响行号表（8176-8178），落地清单第 3 步 |
| 启动脚本报错不可读 → 同学放弃（第一大放弃点） | 高 | §5.6 脚本内置检测 + 中文提示 + 停住；§5.7 自检面板 |
| 单文件变大（+≈400 行邮箱 + 加固） | 低 | 可接受；这是「能整套发出去」的既定取舍 |

---

## 10. 落地顺序（供实施）与端到端验证

**建议顺序**（先修 bug/加固/脚本，再加新功能；每步都跑两套回归）：
1. `config.example.ini` 默认值修正 + `[mail]` 段（用户一拿到就能起，且不自作主张联网）；
2. 三处加固 + 父链（ADR-004/005/006/007）—— 都是已复现缺陷，先堵；
3. `start.command`/`start.bat` Python 检测（§5.6）+ `env_selfcheck` 三处复用（§5.7）—— 直接对第一大放弃点；
4. 邮箱章节内联 + `imap` 数据源 + 向导（ADR-001/002）——
   **先离线**：用 `.eml` 夹具（照搬 `qqmail2table.py:623 load_eml` 与 `SELF_TEST_MAIL` :774）跑规则，再连线；
5. 公司匹配（§4）；
6. 打包脚本 + `snapshot/` + README 安装步骤（ADR-003）；
7. 扩展端口自动探测 + `popup` 端口可配（§5.5/ADR-008）。

**每步必过的回归**：
```bash
python3 applytracker.py --self-test          # 405 项（注意：改父链后断言数/内容会变）
# 关通配 CORS 后新增的安全回归：
#   伪造 Origin: https://evil.com 的 GET/POST/OPTIONS → 必须 403
#   无 Origin 的 POST → 放行；Origin: chrome-extension://... → 放行
# 写入一致性回归：50 并发 add_app → 行数=50；手加一列 → update 后列还在
~/.workbuddy/bin/safe-browser --browser chrome serve --port 9444   # 起浏览器
BROWSER_URL=http://127.0.0.1:9444 node tools/board-check.js       # 132 项
```

**端到端验证（发行版主链路，收尾必跑）**：
1. 从 tag 打一个包到 `/tmp/rel/`，解压；
2. **扫描通过**：包内无 `data/**`、无 `config.ini`、无真实 token/公司名（第 3 步失败即中止）；
3. 在干净目录跑 `python3 applytracker.py --serve` → `config.ini` 自动生成、`data/` 自动建、`snapshot/opportunities.json` 拷入，`env_selfcheck` 逐项打印；
4. **脚本降级验证**：把 `python3` 从 PATH 摘掉跑 `start.command` → 出现中文提示并停住（不闪退）；
   Windows 侧把 `python` 换成不可用 → 同样中文提示 + 停住；
5. **端口自动挑 + 探测验证**：先占住 8788 → 双击 start 脚本应自动用 8789 且黑窗口明确打印「默认端口 8788 被占，服务改跑在 8789」；
   扩展 `at_port` 留 8788 → 扩展自动探到 8789 并写回；再在 8788 起一个非 applytracker 假服务 → 扩展**跳过**该端口（`/api/health` 身份不符）；8788–8791 全占 → 脚本报人话退出、扩展走输入框兜底；
   **另测服务端兜底**（v0.30.0 已落地，不再是缺口）：直接 `python3 applytracker.py --serve`（不经脚本）
   且 8788 被占 → 应自动挪到 8789 并**明确打印**实际端口；`--port 9000` 且 9000 被占 →
   **只在 9000 上试一次就报中文错**，不擅自漂到 9001；
6. 扩展（`at_port` 指向该端口）记一条 → 看板出现该条（本地闭环，无外网）；
7. `python3 applytracker.py --mail-config` 配 QQ 授权码 → `test_login` 成功 → `--notices-sync` 后
   `data/notices.csv` 出现通知，**每条的类别与用户肉眼判断一致**；
8. 一封「认不出公司」的邮件 → 落在「未匹配区」、**没有出现在任何公司的通知里**（验证宁缺毋滥）；
9. 误报回归：造一封同时含「测评」与「面试」的邮件 → 进待确认区，**不标类型**。
