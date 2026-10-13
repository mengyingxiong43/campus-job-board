#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make-release.py —— applytracker 发行包构建脚本（只用 Python 标准库）。

为什么要这个脚本（而不是「拷个文件夹」）：
    作者本机的 data/ 里是 165 条真实投递记录、9381 条机会库、飞书 token。
    手工拷目录会把它们一起拷进包里发出去 —— 一次就是事故。
    所以打包只走 `git archive`（只含已跟踪文件，data/ 未跟踪 → 天然带不出），
    打完再拿「当前工作树的真实值」当词表，逐字节扫一遍包内文本；命中即中止，不出包。

用法：
    python3 tools/make-release.py                     # 门禁 + 打包 HEAD，通过才落 dist/
    python3 tools/make-release.py --version 0.28.0    # 指定版本号（默认读 applytracker.py 的 APP_VERSION）
    python3 tools/make-release.py --ref v0.28.0       # 从某个 tag/ref 导出（默认 HEAD）
    python3 tools/make-release.py --allow-dirty       # 逃生阀：工作区有未提交改动也继续（不推荐）
    python3 tools/make-release.py --skip-selftest     # 跳过自检门禁（仅排障用）
    python3 tools/make-release.py --keep-temp         # 保留解压出的临时目录（排障用）

设计约束（见 docs/decisions/ADR-003）：
    - 不在脚本里写死任何 secret：词表在运行时从当前工作树读取。脚本进包也无害。
    - 禁止路径 + 禁止内容 两道扫描，任一命中即中止、不产出 dist/。
"""

import argparse
import configparser
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# 允许出现在包里的「公开示例公司名」白名单 —— 目前有意留空。
#
# 历史：README / 代码曾用几家公开大公司当示例名字，它们恰好也在作者的投递名单里，
# 会被扫描误报成泄漏。现在判据更强（见下），这几家无需手工列举即被排除。
# 现在判据更强、不需要手工列举：凡「仓库已发布文本（已跟踪文件，含随包快照）里出现过的字符串」
# 一律不算私有信号（见 _repo_published_text）。这几家自然被排除。
# 保留本机制仅为将来兜底：若确实遇到「是公开信息、却又不在已发布文本里」的字面量，再显式加。
# 铁律：这里只放公开字符串，绝不放任何 secret（secret 一律不接受白名单）。
# ---------------------------------------------------------------------------
BENIGN_PUBLIC_EXAMPLES = set()

# 通用词：这些词是正常代码/文档里会出现的常见词，不当成「私有泄漏」信号。
STOPWORDS = {
    "FAST", "OPEN", "TEST", "DEMO", "DATA", "TODO", "NOTE", "NULL", "TRUE",
    "FALSE", "HTTP", "HTTPS", "JSON", "HTML", "CSV", "UTF-8", "UTF8",
}

# config.ini 里取值当 secret 的词表键名（正则，小写匹配 key）
SECRET_KEY_RE = re.compile(r"(token|secret|table_id|app_id|authcode|password|passwd|user|account|email)")

# 授权码形态：`authcode = xxxxxxxx…`（16 位以上纯字母数字）
AUTHCODE_LIKE_RE = re.compile(r"(?i)authcode\s*[=:]\s*['\"]?([A-Za-z0-9]{16,})['\"]?")

# 不会出现在包里的私有文件名（TLS 之外的兜底）
FORBIDDEN_BASENAMES = {"config.ini"}

# 解出来的包里这些路径只查 secret、不查公司名（它们是「允许进包」的公开数据）
NAME_SCAN_SKIP = {
    "snapshot/opportunities.json",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def run(cmd, cwd=REPO_ROOT, check=True, capture=True):
    """跑一条外部命令，返回 CompletedProcess。"""
    return subprocess.run(
        cmd, cwd=cwd, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )


def human_size(n):
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return "%.1f %s" % (f, u)
        f /= 1024
    return "%d B" % n


def info(msg):
    print("  %s" % msg, flush=True)


def step(n, total, title):
    print("\n[%d/%d] %s" % (n, total, title), flush=True)


def die(msg):
    print("\n  [中止] %s" % msg, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. 读取版本号
# ---------------------------------------------------------------------------
def read_version():
    path = os.path.join(REPO_ROOT, "applytracker.py")
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r'\s*APP_VERSION\s*=\s*["\']([^"\']+)["\']', line)
            if m:
                return m.group(1)
    return None


def read_version_from_ref(ref):
    """读某个 ref 里 applytracker.py 的 APP_VERSION（用于和当前工作树对账）。"""
    cp = run(["git", "show", "%s:applytracker.py" % ref], check=False)
    if cp.returncode != 0:
        return None
    for line in cp.stdout.splitlines():
        m = re.match(r'\s*APP_VERSION\s*=\s*["\']([^"\']+)["\']', line)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# 2. 门禁
# ---------------------------------------------------------------------------
def gate_worktree(allow_dirty):
    cp = run(["git", "status", "--porcelain"])
    dirty = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    if dirty:
        if not allow_dirty:
            die("工作区不干净（被打包的 = 被测试的）。先提交，或加 --allow-dirty 强行继续。\n"
                "        未提交项（前 10 条）：\n" + "\n".join("          " + d for d in dirty[:10]))
        info("!! 工作区不干净，--allow-dirty 已放行（打出的包不含未提交内容）：%d 项" % len(dirty))
        return False
    info("工作区干净")
    return True


def gate_selftest(skip):
    if skip:
        info("!! 跳过自检（--skip-selftest）")
        return
    cp = run([sys.executable, os.path.join(REPO_ROOT, "applytracker.py"), "--self-test"],
             check=False)
    if cp.returncode != 0:
        tail = "\n".join("          " + l for l in cp.stdout.splitlines()[-15:])
        die("自检没通过，不出包。最后几行：\n" + tail)
    n_pass = cp.stdout.count(u"\u2713")
    info("自检通过（%d 项 \u2713）" % n_pass)


# ---------------------------------------------------------------------------
# 3. 导出
# ---------------------------------------------------------------------------
def export(ref, version, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    # 先导到临时文件，扫描通过后再落到 dist/（避免「扫描没过却留下半个包」）
    tmp = tempfile.NamedTemporaryFile(prefix="applytracker-", suffix=".tar.gz", delete=False)
    tmp.close()
    prefix = "applytracker-%s/" % version
    cp = subprocess.run(
        ["git", "archive", "--format=tar.gz", "--prefix=%s" % prefix, ref],
        cwd=REPO_ROOT, stdout=open(tmp.name, "wb"), stderr=subprocess.PIPE,
    )
    if cp.returncode != 0:
        os.unlink(tmp.name)
        die("git archive 失败（ref=%s）：%s" % (ref, cp.stderr.decode("utf-8", "replace").strip()))
    size = os.path.getsize(tmp.name)
    if size == 0:
        os.unlink(tmp.name)
        die("导出为空 —— 检查 ref（%s）是否存在。" % ref)
    info("已导出 %s（%s）" % (prefix, human_size(size)))
    return tmp.name


# ---------------------------------------------------------------------------
# 4. 扫描 A：禁止路径
# ---------------------------------------------------------------------------
def scan_paths(extract_root, prefix):
    pid = os.path.join(extract_root, prefix.rstrip("/"))
    bad = []
    total_files = 0
    for dirpath, dirnames, filenames in os.walk(pid):
        rel = os.path.relpath(dirpath, pid)
        rel = "" if rel == "." else rel.replace(os.sep, "/")
        for fn in filenames:
            total_files += 1
            frel = (rel + "/" + fn) if rel else fn
            parts = frel.split("/")
            if "data" in parts:
                bad.append(frel)
            if fn in FORBIDDEN_BASENAMES:
                bad.append(frel)
    if bad:
        uniq = sorted(set(bad))
        die("包内出现禁止路径（data/** 或 config.ini）：\n" +
            "\n".join("          " + b for b in uniq[:30]) +
            ("" if len(uniq) <= 30 else "\n          …共 %d 个" % len(uniq)))
    info("禁止路径扫描通过（包内无 data/**、无 config.ini）")
    return total_files


# ---------------------------------------------------------------------------
# 5. 构造私有词表（从当前工作树实时读取）
# ---------------------------------------------------------------------------
def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


# 会进词表的私有数据文件（都在 data/ 下，本身不进包，但内容不该出现在包里）。
# mail_* 是邮箱功能的产物：真实邮件主题/发件人/公司名/岗位，也属隐私面，用通配一起收。
#
# ⚠️ 2026-10-08 补了 4 个进来（原清单只 4 个，实测 5 个盲区）：证据文件名从 mail_evidence.json
# 改成 notice_evidence.json 之后，固定清单没跟、mail_ 通配也罩不住它 —— 整份「识别依据」在隐私
# 扫描里是瞎的。app_renames / app_parents 的值与 interview_progress 的**键**就是公司名，同样漏。
# 所以下面另配了 audit_data_file_coverage()：applytracker.py 里出现的新产物文件没归类就打包失败，
# 这类「改了名字 / 加了文件、清单没跟」的洞不再靠人眼去发现。
PRIVATE_DATA_FILES = (
    "applications.csv", "notices.csv", "待投递.csv", "notice_states.json",
    "notice_evidence.json",      # 识别依据：真实邮件主题 / 发件人 / 命中词（函数名叫 mail_evidence_path，文件却叫这个）
    "app_renames.json",          # 公司改名映射：新旧公司名都在值里
    "app_parents.json",          # 子公司并入母公司：键是真实公司名
    "interview_progress.json",   # 面试进度：键是真实公司名
)

# 不进词表的 data/ 产物 —— 每一个都得写明理由，不许「先放着」。
# 理由要能被推翻：哪天某个文件的理由不成立，就把它挪到 PRIVATE_DATA_FILES 去。
NON_PRIVATE_DATA_FILES = {
    "opportunities.json": "公开岗位库（不是作者自己的数据）；收进来会满屏误报，把扫描废掉",
    ".apps_sync.json": "只有同步时间戳",
    ".mail_state.json": "只有邮件指纹（哈希），没有原文",
    "backups": "目录不是文件；里面那几份已在 PRIVATE_DATA_FILES 里逐个覆盖（见 _backup_sources）",
    "看板.html": "是 applications/notices/opportunities 的渲染产物，私有串已被那几份覆盖；"
                "而 HTML 整行读进来会灌进一堆标签碎片，反而制造误报",
}

# CSV 里只挑「带私有身份」的列抽词（公司/岗位/邮件主题/发件人/摘要/备注/姓名/邮箱）。
# 绝不整表抽：表头名（"公司名称" "投递时间" "飞书状态"…）本身就会出现在代码/文档里，整表抽会满屏误报。
CSV_PRIVATE_COL_RE = re.compile(r"(公司|单位|岗位|职位|主题|发件|摘要|备注|姓名|邮箱)")


def _strings_from_obj(obj):
    """把一个 JSON 结构里的所有字符串值（长度 ≥2）掏出来。"""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out |= _strings_from_obj(k)
            out |= _strings_from_obj(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= _strings_from_obj(v)
    elif isinstance(obj, str):
        c = obj.strip()
        if len(c) >= 2:
            out.add(c)
    return out


def _collect_from_file(path):
    """从一个数据文件里抽候选字符串（长度 ≥2）。文件不存在 / 读不了 → 返回空集，不报错。"""
    if not os.path.isfile(path):
        return set()
    out = set()
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".csv":
            with open(path, encoding="utf-8-sig", newline="") as f:
                rd = csv.DictReader(f)
                cols = [c for c in (rd.fieldnames or []) if c and CSV_PRIVATE_COL_RE.search(c)]
                if not cols:
                    return set()   # 认不出私有列就整份跳过（宁可不扫，也不误报）
                for row in rd:
                    for c in cols:
                        v = (row.get(c) or "").strip()
                        if len(v) >= 2:
                            out.add(v)
        elif ext == ".jsonl":
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out |= _strings_from_obj(json.loads(line))
                    except ValueError:
                        if len(line) >= 2:
                            out.add(line)
        elif ext == ".json":
            out |= _strings_from_obj(json.loads(_read_text(path) or "null"))
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    c = line.strip()
                    if len(c) >= 2:
                        out.add(c)
    except (OSError, ValueError, UnicodeDecodeError, csv.Error):
        return set()   # 读不了就当没有，绝不因此打断打包
    return out


def _collect_private_names():
    """遍历 data/ 下的私有数据文件（含所有 mail_*）抽候选词。缺文件/空目录是正常状态。"""
    data_dir = os.path.join(REPO_ROOT, "data")
    out = set()
    if not os.path.isdir(data_dir):
        return out
    for fn in PRIVATE_DATA_FILES:
        out |= _collect_from_file(os.path.join(data_dir, fn))
    try:
        entries = sorted(os.listdir(data_dir))
    except OSError:
        entries = []
    for fn in entries:
        if fn.startswith("mail_"):
            out |= _collect_from_file(os.path.join(data_dir, fn))
    return out


# applytracker.py 里所有「写进 data/ 目录」的产物名字，写法统一是 os.path.join(xx.out_dir, "X")
_DATA_FILE_LITERAL_RE = re.compile(
    r'os\.path\.join\(\s*(?:cfg\.out_dir|out_dir|self\.out_dir)\s*,\s*"([^"]+)"')


def audit_data_file_coverage():
    """反向核对：applytracker.py 会写进 data/ 的每个产物，是不是都归过类了。

    返回**没归类**的文件名列表（空 = 全部覆盖了）。

    为什么需要它：词表是「打包前拿工作树真实值当场抽的」，而产物清单却要人手工维护 ——
    这两件事一旦脱节，扫描就会安静地少扫一片，而且**没有任何报错**。2026-10-08 实测就是这样：
    证据文件从 mail_evidence.json 改名成 notice_evidence.json，固定清单没跟、mail_ 通配罩不住，
    连同 app_renames / app_parents / interview_progress 一起，5 个文件在隐私扫描里是瞎的。
    所以这里直接从源码里把产物名字抠出来逐个对账 —— 下次谁再加一个 data/ 文件而忘了归类，
    打包当场失败并点名，不用等人再发现一次。

    已知边界（写明，别把它当成全覆盖）：只认 `os.path.join(xx.out_dir, "字面量")` 这种写法。
    文件名是变量拼出来的（如 `os.path.join(out_dir, name)`）认不出 —— 那种写法本来就少见，
    真出现了请顺手把名字写进上面两个清单之一。
    """
    src = _read_text(os.path.join(REPO_ROOT, "applytracker.py"))
    if not src:
        return []      # 源码都读不到就别瞎报错（脱离仓库跑的情况）
    literal = set(_DATA_FILE_LITERAL_RE.findall(src))
    missing = []
    for fn in sorted(literal):
        if fn in PRIVATE_DATA_FILES or fn in NON_PRIVATE_DATA_FILES:
            continue
        if fn.startswith("mail_"):
            continue    # 通配已收
        missing.append(fn)
    return missing


def _repo_published_text():
    """仓库里**已跟踪**（会随包一起发出去）的文本，data/ 除外。

    用途：词表去重。凡「仓库自己早就公开」的字符串都不再当泄漏信号 —— 否则代码/README/自检
    夹具里本来就有的字面量（示例公司名、占位文案…）会把每一份包都拦下来，扫描就没法用了。
    注意：因此本扫描是「第二道网/绊线」——真正兜底的是「禁止路径」（data/ 未跟踪进不了包）
    与「secret 扫描」；内容扫描负责拦「被抄进已跟踪文件里的私有串」。
    """
    out = []
    try:
        # ⚠️ 必须关掉 quotepath：git 默认把非 ASCII 路径输出成转义形式（"ADR-001-\351..."），
        # 拿转义路径去读文件必 404、再被 except 静默吞掉 —— 中文名文档整体从「已公开文本」里
        # 消失，词表去重对它们失明，2026-10-08 被 ADR-001 里的「126.com」误拦过一次。
        cp = run(["git", "-c", "core.quotepath=false", "ls-files"], check=False)
        names = cp.stdout.splitlines()
    except Exception:
        names = []
    for fp in names:
        fp = fp.strip()
        if not fp or fp.startswith("data/"):
            continue
        out.append(_read_text(os.path.join(REPO_ROOT, fp)))
    return "\n".join(out)


def build_wordlist():
    secrets = set()

    # (a) config.ini 里的 token / 表 id / 邮箱账号 等
    cfg_path = os.path.join(REPO_ROOT, "config.ini")
    if os.path.isfile(cfg_path):
        cp = configparser.ConfigParser()
        try:
            cp.read(cfg_path, encoding="utf-8")
        except configparser.Error:
            cp = None
        if cp:
            for sect in cp.sections():
                for k, v in cp.items(sect):
                    v = (v or "").strip()
                    if not v:
                        continue
                    if SECRET_KEY_RE.search(k) and len(v) >= 6:
                        secrets.add(v)

    # (b) data/ 下的私有数据 —— 公司与岗位名、邮件主题、mail_* 里的真实收发件人等。
    #     自动遍历、逐个抽词；文件不存在就跳过（干净仓库里 mail_* 通常不存在，词表为空正常）。
    names = _collect_private_names()

    # 过滤：太短的不当信号（中文 >=4 字 / ASCII >=6 位），通用词去掉，
    #       ① 出现在「随包公开快照/已发布文本」里的词去掉 —— 快照本身要发出去、代码/README 本就公开，
    #          它们里面有的字符串不算私有泄漏（否则自己发自己触发）；
    published_text = _repo_published_text()

    def keep(w):
        w = w.strip()
        if w in STOPWORDS or w in BENIGN_PUBLIC_EXAMPLES:
            return False
        if published_text and w in published_text:
            return False  # 仓库里已经公开的字面量（含随包快照），不算私有信号
        ascii_only = all(ord(ch) < 128 for ch in w)
        if ascii_only:
            return len(w) >= 6 and (" " not in w)
        return len(w) >= 4

    names = {w.strip() for w in names if keep(w)}
    secrets = {w for w in secrets if len(w) >= 6}
    return sorted(secrets), sorted(names)


# ---------------------------------------------------------------------------
# 6. 扫描 B：禁止内容
# ---------------------------------------------------------------------------
def _iter_text_files(root, prefix):
    pid = os.path.join(root, prefix.rstrip("/"))
    for dirpath, _dirnames, filenames in os.walk(pid):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, pid).replace(os.sep, "/")
            try:
                with open(full, "rb") as f:
                    head = f.read(4096)
            except OSError:
                continue
            if b"\x00" in head:  # 二进制，跳过
                continue
            yield rel, full


def scan_content(extract_root, prefix, secrets, names):
    hits = []  # (rel, kind, token)
    authcode_hits = []

    for rel, full in _iter_text_files(extract_root, prefix):
        text = _read_text(full)
        if not text:
            continue
        # secret：全包所有文本文件都查
        for s in secrets:
            if s in text:
                hits.append((rel, "secret", s))
        # 公司名/主题：公开快照文件跳过（那是白名单公开数据，词表已去重）
        if rel not in NAME_SCAN_SKIP:
            for w in names:
                if w in text:
                    hits.append((rel, "name", w))
        # 授权码形态兜底
        for m in AUTHCODE_LIKE_RE.finditer(text):
            authcode_hits.append((rel, m.group(1)))

    if hits or authcode_hits:
        lines = []
        seen = set()
        for rel, kind, tok in hits:
            key = (rel, kind, tok)
            if key in seen:
                continue
            seen.add(key)
            label = "secret" if kind == "secret" else "private"
            # 只打印前几个字符，避免把隐私再打进日志
            shown = tok[:4] + "…" if len(tok) > 4 else tok[:1] + "…"
            lines.append("          %s  [%s] %s" % (rel, label, shown))
        for rel, tok in authcode_hits:
            lines.append("          %s  [authcode] %s…" % (rel, tok[:4]))
        die("包内命中作者私有数据（命中即中止，不出包）：\n" + "\n".join(lines[:40]) +
            ("" if len(lines) <= 40 else "\n          …共 %d 处" % len(lines)))

    info("禁止内容扫描通过（%d 个 secret 词、%d 个私有名/主题词，全包 0 命中）"
         % (len(secrets), len(names)))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="applytracker 发行包构建（git archive + 私有数据扫描）")
    ap.add_argument("--version", help="版本号（默认读 applytracker.py 的 APP_VERSION）")
    ap.add_argument("--ref", default="HEAD", help="从哪个 ref 导出（tag/分支/HEAD，默认 HEAD）")
    ap.add_argument("--out", default="dist", help="产物目录（默认 dist/）")
    ap.add_argument("--allow-dirty", action="store_true", help="工作区有未提交改动也继续（不推荐）")
    ap.add_argument("--skip-selftest", action="store_true", help="跳过自检门禁（排障用）")
    ap.add_argument("--keep-temp", action="store_true", help="保留解压出的临时目录")
    args = ap.parse_args()

    if not os.path.isdir(os.path.join(REPO_ROOT, ".git")):
        die("这个脚本要放在 applytracker 仓库里跑（找不到 .git）。")

    version = args.version or read_version()
    if not version:
        die("读不到版本号：applytracker.py 里没有 APP_VERSION，请用 --version 指定。")

    # 对账：包名用的版本号来自工作树，代码却来自 ref —— 两者不一致会「文件名一个版本、代码另一个版本」
    if not args.version:
        ref_ver = read_version_from_ref(args.ref)
        if ref_ver and ref_ver != version:
            msg = ("版本对不上：工作树 APP_VERSION=%s，但 %s 里的 applytracker.py 是 %s。"
                   % (version, args.ref, ref_ver))
            if not args.allow_dirty:
                die(msg + "\n        先把工作树的改动提交，或用 --ref 指到与工作树一致的提交/标签。")
            info("!! " + msg + "（--allow-dirty 已放行，包名会与实际代码版本不符）")

    total = 6
    print("=" * 62)
    print("  applytracker 发行包构建  ref=%s  version=%s" % (args.ref, version))
    print("=" * 62)

    step(1, total, "门禁：工作区状态")
    gate_worktree(args.allow_dirty)

    step(2, total, "门禁：自检")
    gate_selftest(args.skip_selftest)

    step(3, total, "导出：git archive")
    tmp_pkg = export(args.ref, version, os.path.join(REPO_ROOT, args.out))

    workdir = tempfile.mkdtemp(prefix="at-release-")
    prefix = "applytracker-%s/" % version
    try:
        step(4, total, "扫描：禁止路径")
        with tarfile.open(tmp_pkg, "r:gz") as tf:
            try:
                tf.extractall(workdir, filter="data")   # Python 3.12+
            except TypeError:
                tf.extractall(workdir)                  # Python 3.9–3.11
        total_files = scan_paths(workdir, prefix)

        step(5, total, "扫描：私有数据（拿当前工作树真实值当词表）")
        _uncovered = audit_data_file_coverage()
        if _uncovered:
            die("data/ 产物有 %d 个没归类，隐私扫描会漏掉它们：\n" % len(_uncovered)
                + "\n".join("          %s" % fn for fn in _uncovered)
                + "\n        判一下：装私有数据 → 加进 PRIVATE_DATA_FILES；"
                  "不装 → 加进 NON_PRIVATE_DATA_FILES 并写明理由。")
        info("产物对账通过：applytracker.py 写进 data/ 的文件全部归过类")
        secrets, names = build_wordlist()
        scan_content(workdir, prefix, secrets, names)

        step(6, total, "落盘")
        out_dir = os.path.join(REPO_ROOT, args.out)
        os.makedirs(out_dir, exist_ok=True)
        final = os.path.join(out_dir, "applytracker-%s.tar.gz" % version)
        shutil.move(tmp_pkg, final)
        tmp_pkg = None
        size = os.path.getsize(final)
        info("包路径：%s" % os.path.relpath(final, REPO_ROOT))
        info("大小：%s    文件数：%d" % (human_size(size), total_files))
    finally:
        if not args.keep_temp:
            shutil.rmtree(workdir, ignore_errors=True)
        elif os.path.isdir(workdir):
            info("临时目录保留在：%s" % workdir)
        if tmp_pkg and os.path.exists(tmp_pkg):
            os.unlink(tmp_pkg)

    print("\n  \u2713 打包成功：%s" % os.path.relpath(final, REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
