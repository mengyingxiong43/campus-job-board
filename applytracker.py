#!/usr/bin/env python3
"""
applytracker — 网申投递进度看板 + 邮箱通知看板，一个页面两个标签。

- 投递记录：浏览器扩展在你点完「提交」后自动弹确认，记下公司 / 岗位 / 链接 / 时间
- 邮箱通知：直连你自己的邮箱（IMAP）收校招通知，或读一份现成的 CSV / 飞书表，
  按公司名跟投递记录对上号
- 机会库：从一张飞书汇总表同步校招 / 实习岗位（企业、岗位、截止时间、投递入口）
- 数据全部落在本机 data/ 目录，不联网（收邮件 / 同步飞书时才联网）、不需要账号

用法：
    python3 applytracker.py --serve          # 起本地看板（推荐）
    python3 applytracker.py --mail-config    # 配自己的邮箱（IMAP）：填邮箱 + 授权码
    python3 applytracker.py --notices-sync   # 手动收一次邮件
    python3 applytracker.py --add --company 云溪智能 --position 内容运营 --url https://...
    python3 applytracker.py --list
    python3 applytracker.py --opps-sync      # 手动同步一次机会库
    python3 applytracker.py --self-test

只依赖标准库。
"""

from __future__ import annotations  # noqa: F401  (3.9 上也能跑)

import argparse
import base64
import configparser
import csv
import email
import email.utils
import hashlib
import imaplib
import json
import os
import quopri
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

APP_VERSION = "0.35.1"
DEFAULT_PORT = 8788
# 端口兜底区间：默认从 8788 起，8788–8791 里挑第一个能起来的（见 pick_port）。
# ⚠️ 这两个数必须和 start.command 顶上的 BASE / LAST 对齐 —— 那边还有一份（自检会逐字对）。
PORT_BASE = DEFAULT_PORT
PORT_LAST = 8791

# 状态是固定几个，看板和扩展下拉都用这一份，免得两边漂移
STATUSES = ["已投递", "简历通过", "测评笔试", "面试", "Offer", "已结束"]
STATUS_ALIAS = {
    "已申请": "已投递", "投递成功": "已投递", "已网申": "已投递",
    "笔试": "测评笔试", "测评": "测评笔试", "在线测评": "测评笔试",
    "一面": "面试", "二面": "面试", "终面": "面试", "约面": "面试",
    "offer": "Offer", "录用": "Offer", "意向书": "Offer",
    "感谢信": "已结束", "已挂": "已结束", "已拒": "已结束", "流程结束": "已结束",
}
# 「飞书状态」这一列存的是飞书表里那一格的**原文**（"待一面"这种），
# 不是翻译后的 6 个标准状态。有它才能分辨「飞书那边动过」和「我在看板上手改的」——
# 否则每次同步都会把你手改的状态按回飞书的老值。
CSV_FIELDS = ["id", "公司名称", "岗位名称", "状态", "飞书状态", "投递时间", "网页链接",
              "来源", "备注", "更新时间"]
# 看板上「点一下就地改」的字段白名单：请求参数名 → CSV 列名。
# 只认这几个，别的（尤其 id / 来源 / 飞书状态）一概不收 —— 手滑传进来也不能改身份列。
APP_EDIT_FIELDS = {
    "company": "公司名称",
    "position": "岗位名称",
    "status": "状态",
    "applied_at": "投递时间",
    "url": "网页链接",
    "note": "备注",
}
# 状态分组，用来算「进行中 / 已约面」这类汇总
ACTIVE_STATUSES = {"已投递", "简历通过", "测评笔试"}
INTERVIEW_STATUSES = {"面试"}
OFFER_STATUSES = {"Offer"}
CLOSED_STATUSES = {"已结束"}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def resolve_config_path(base: str, cfg_path: str) -> str:
    """--base 指过去时，config.ini 优先在那个目录里找，**哪怕当前目录也有一份**。

    「数据根目录」这个说法得是自洽的：数据、配置都在它下面，指到哪儿就用哪儿的。
    2026-10-02 踩过一个很脏的坑 —— 原来只判「当前目录没有 config.ini 才去 base 里找」，
    而跑演示截图时人就在仓库目录里（仓库有 config.ini），于是那句 `--base /tmp/at-demo`
    被当成了「用演示数据目录 + 用仓库这份配置」，仓库那份配置里 `[applications] source = feishu`
    是真表 token → 演示服务把真实投递记录同步进了演示目录，截图里直接出现真实公司名。
    判断依据得是「谁指得更明确」，--base 比 cwd 明确，所以 base 优先。
    """
    if not base or not cfg_path or os.path.isabs(cfg_path):
        return cfg_path
    cand = os.path.join(base, cfg_path)
    return cand if os.path.exists(cand) else cfg_path


class Config:
    def __init__(self, path: Optional[str] = None, base: Optional[str] = None):
        self.path = path or ""
        self.base = base or os.path.dirname(os.path.abspath(__file__))
        cp = configparser.ConfigParser()
        if self.path and os.path.exists(self.path):
            cp.read(self.path, encoding="utf-8")

        out_dir = self._get(cp, "output", "dir", "data")
        if not os.path.isabs(out_dir):
            out_dir = os.path.join(self.base, out_dir)
        self.out_dir = out_dir
        self.app_csv = self._path(cp, "output", "applications_csv", os.path.join(out_dir, "applications.csv"))
        # 邮箱通知的 CSV：config.ini 里没写就自动找隔壁 qqmail2table 的默认输出，
        # 找不到也没关系，看板会提示怎么接上
        # 邮箱直连（IMAP）—— 见 ADR-001。
        #   凭据齐（user + authcode 都有）就算「配好了」，不再要求额外翻一个开关：
        #   填了邮箱和授权码的人，意图已经很明确（2026-10-02 那句「填了就该生效」）。
        #   [mail] source 只留一个「彻底关掉」的出口：显式写 off 就不连，哪怕凭据在。
        #   authcode 支持环境变量 APPLYTRACKER_MAIL_AUTHCODE，优先于明文（打包/存盘时按敏感串处理）。
        self.mail_source = self._get(cp, "mail", "source", "auto").strip().lower()
        if self.mail_source not in ("off", "imap", "auto"):
            self.mail_source = "auto"
        self.mail_host = self._get(cp, "mail", "host", "imap.qq.com")
        self.mail_port = max(1, self._get_int(cp, "mail", "port", 993))
        self.mail_user = self._get(cp, "mail", "user", "")
        self.mail_authcode = (os.environ.get("APPLYTRACKER_MAIL_AUTHCODE", "").strip()
                              or self._get(cp, "mail", "authcode", ""))
        self.mail_folder = self._get(cp, "mail", "folder", "INBOX")
        self.mail_days_back = max(1, self._get_int(cp, "mail", "days_back", 30))
        self.mail_only_unseen = self._get_bool(cp, "mail", "only_unseen", False)
        self.mail_ttl = max(60, self._get_int(cp, "mail", "ttl", 900))
        self.mail_configured = bool(self.mail_user and self.mail_authcode)
        self.mail_enabled = self.mail_configured and self.mail_source != "off"
        self.mail_state_json = os.path.join(out_dir, ".mail_state.json")

        # 邮箱通知从哪来：
        #   feishu = 直接从飞书多维表格拉（适合已经在用定时任务往飞书写的那套）
        #   imap   = 本机直连邮箱自己抓（ADR-001，配了 [mail] user+authcode 才走得通）
        #   file   = 读一个现成的 CSV（qqmail2table 的产物）
        #   auto   = 配了邮箱就走 imap，否则配了飞书表就走飞书，否则有 CSV 就用 CSV
        #   off    = 都不接（看板上给「怎么配」的提示，不报错）
        self.feishu_base = self._get(cp, "notices", "feishu_base_token", "")
        self.feishu_table = self._get(cp, "notices", "feishu_table_id", "")
        self.feishu_profile = self._get(cp, "notices", "feishu_profile", "")
        self.feishu_ttl = max(30, self._get_int(cp, "notices", "feishu_ttl", 300))
        src = self._get(cp, "notices", "source", "auto").strip().lower()
        if src not in ("auto", "file", "feishu", "imap", "off"):
            src = "auto"
        explicit = self._get(cp, "notices", "csv", "")
        guess = self._guess_notices()
        if src == "auto":
            if self.mail_enabled:
                src = "imap"
            elif self.feishu_base and self.feishu_table:
                src = "feishu"
            else:
                src = "file" if (explicit or guess) else "off"
        if src == "imap" and not self.mail_enabled:
            # 嘴上说要 imap，但 [mail] 里没配齐凭据 → 静默退回能找到的 CSV，别让看板打不开
            src = "file" if (explicit or guess) else "off"
        self.notices_source = src
        if src in ("feishu", "imap"):
            # 飞书 / 邮箱模式下这个 CSV 是本地缓存，每次同步覆盖（imap 是「追加 + 去重」）写
            self.notices_csv = self._path_opt(explicit) or os.path.join(out_dir, "notices.csv")
        elif src == "file":
            self.notices_csv = self._path_opt(explicit) or guess
        else:
            self.notices_csv = ""

        # 岗位机会库：另一张（很大的）飞书表，别人整理好的招聘信息汇总
        self.opp_base = self._get(cp, "opportunities", "feishu_base_token", "")
        self.opp_table = self._get(cp, "opportunities", "feishu_table_id", "")
        self.opp_profile = self._get(cp, "opportunities", "feishu_profile", "") or self.feishu_profile
        self.opp_ttl = max(60, self._get_int(cp, "opportunities", "feishu_ttl", 21600))
        osrc = self._get(cp, "opportunities", "source", "auto").lower()
        if osrc not in ("auto", "file", "feishu", "off"):
            osrc = "auto"
        if osrc == "auto":
            osrc = "feishu" if (self.opp_base and self.opp_table) else "off"
        self.opps_source = osrc
        self.opps_json = os.path.join(out_dir, "opportunities.json")

        # 投递进度：以本地 CSV 为准，但飞书那张表是「进度的真相」——
        # 你在飞书里把某条改成「待一面」，看板上跟着变。同步周期见 apps_ttl。
        self.app_base = self._get(cp, "applications", "feishu_base_token", "")
        self.app_table = self._get(cp, "applications", "feishu_table_id", "")
        self.app_profile = self._get(cp, "applications", "feishu_profile", "") or self.feishu_profile
        asrc = self._get(cp, "applications", "source", "auto").strip().lower()
        if asrc == "auto":
            asrc = "feishu" if (self.app_base and self.app_table) else "off"
        self.apps_source = asrc
        self.apps_ttl = max(60, self._get_int(cp, "applications", "feishu_ttl", 1800))
        self.apps_sync_json = os.path.join(out_dir, ".apps_sync.json")
        self.pending_csv = os.path.join(out_dir, "待投递.csv")

        self.port = self._get_int(cp, "server", "port", DEFAULT_PORT)
        self.timezone_offset = self._get_int(cp, "output", "timezone_offset", 8)

    def _guess_notices(self) -> str:
        for rel in ("../qqmail-job-tracker/out/notifications.csv",
                    "../qqmail-job-tracker/data/notifications.csv",
                    "data/notifications.csv"):
            p = os.path.normpath(os.path.join(self.base, rel))
            if os.path.exists(p):
                return p
        return ""

    # -- 小工具 --
    @staticmethod
    def _get(cp: configparser.ConfigParser, sec: str, key: str, default: str) -> str:
        try:
            return cp.get(sec, key).strip() or default
        except Exception:
            return default

    @staticmethod
    def _get_int(cp: configparser.ConfigParser, sec: str, key: str, default: int) -> int:
        try:
            return int(cp.get(sec, key).strip())
        except Exception:
            return default

    @staticmethod
    def _get_bool(cp: configparser.ConfigParser, sec: str, key: str, default: bool) -> bool:
        """true/1/yes/on（不分大小写）算真；没写或写坏了取默认值。"""
        try:
            v = cp.get(sec, key).strip().lower()
        except Exception:
            return default
        if v in ("1", "true", "yes", "y", "on"):
            return True
        if v in ("0", "false", "no", "n", "off"):
            return False
        return default

    def _path(self, cp, sec, key, default):
        v = self._get(cp, sec, key, default)
        return v if os.path.isabs(v) else os.path.join(self.base, v)

    def _path_opt(self, v: str) -> str:
        if not v:
            return ""
        return v if os.path.isabs(v) else os.path.join(self.base, v)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 投递记录（CSV 存储）
# ---------------------------------------------------------------------------
# 保护 applications.csv「读—改—写」的文件级互斥锁。
# 服务是多线程的（ThreadingServer），以前 load → 改内存 → _write_all 全量覆盖却**没有锁**，
# 并发即丢写（实测 10 并发只进 3 条，多半就是「投了没记」的成因）。
# 用 RLock 是故意的：update_app 在临界区里还会调 note_app_rename，同线程要能再拿一次锁。
APP_FILE_LOCK = threading.RLock()


def load_apps(cfg: Config) -> List[Dict[str, Any]]:
    """读投递记录。文件不存在或读坏了都返回空列表，不抛异常。

    **不丢列**：CSV 里出现了什么列就读什么列 —— 她自己在 Excel 里加过的列
    （「内推人」这类）以前会被静默抹掉，现在连**列序**一起保留（dict 记插入序，
    照原表头顺序填进去），_write_all 再照这个顺序写回。
    """
    if not os.path.exists(cfg.app_csv):
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(cfg.app_csv, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields = [k for k in (reader.fieldnames or []) if k]
            for r in reader:
                if not (r.get("公司名称") or "").strip():
                    continue
                row: Dict[str, Any] = {k: (r.get(k) or "") for k in fields}
                for k in CSV_FIELDS:                 # 标准列补齐，读回来永远有这十个键
                    row.setdefault(k, "")
                rows.append(row)
    except OSError:
        return []
    return rows


def _app_fieldnames(rows: List[Dict[str, Any]]) -> List[str]:
    """写出列序：沿用行里已有的列（即原文件列序，含自定义列），缺的标准列补在末尾。

    行是 load_apps 按原表头顺序建出来的，所以第一行 dict 的键序就是原来的列序；
    新加的行只有 CSV_FIELDS 那十个键，不会反过来把顺序带乱。
    """
    fields: List[str] = []
    for r in rows:
        for k in r:
            if k and k not in fields:
                fields.append(k)
    for k in CSV_FIELDS:
        if k not in fields:
            fields.append(k)
    return fields


def _write_all(cfg: Config, rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(cfg.app_csv)), exist_ok=True)
    fields = _app_fieldnames(rows)
    tmp = cfg.app_csv + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    os.replace(tmp, cfg.app_csv)


def mutate_apps(cfg: Config, fn) -> Any:
    """对 applications.csv 做一次「读—改—写」，全程持 APP_FILE_LOCK，写入串行、不丢写。

    fn(rows) 就地改 rows 并返回需要的结果，返回值原样透出。
    add_app / update_app / delete_app / 飞书同步 / --import-apps 全走这里，
    保证「后台同步」和「她在看板上手改」不会互相覆盖（ADR-005）。
    """
    with APP_FILE_LOCK:
        rows = load_apps(cfg)
        result = fn(rows)
        _write_all(cfg, rows)
        return result


def norm_status(s: str) -> str:
    s = (s or "").strip()
    if s in STATUSES:
        return s
    key = s.lower()
    for alias, real in STATUS_ALIAS.items():
        if alias.lower() == key:
            return real
    for real in STATUSES:
        if real in s:
            return real
    return "已投递"


def norm_applied_at(s: str) -> str:
    """投递时间统一成 `2026-10-04 13:34`。
    页面上的时间选择器给的是 `2026-10-04T13:34`，只写日期的补 00:00。
    **认得出的才动**：她手写的「10 月初」这种原样留着 —— 别把她的字吃掉，
    认不出最多是排序时沉底（daysSince 返回 null，页面显示「—」），不会报错。
    """
    t = (s or "").strip()
    if not t:
        return ""
    head = t[:16] if len(t) >= 16 else t
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(head, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return t[:40]


def find_dup(rows: List[Dict[str, Any]], company: str, position: str, url: str,
             minutes: int = 10) -> Optional[Dict[str, Any]]:
    """同公司+同岗位（或同链接）在 N 分钟内重复提交，算同一次投递。"""
    c, p = (company or "").strip(), (position or "").strip()
    cutoff = datetime.now() - timedelta(minutes=minutes)
    for r in reversed(rows):
        if (r.get("公司名称") or "").strip() != c:
            continue
        same_pos = (r.get("岗位名称") or "").strip() == p
        same_url = url and r.get("网页链接") == url
        if not (same_pos or same_url):
            continue
        try:
            if datetime.strptime(r.get("投递时间", ""), "%Y-%m-%d %H:%M") >= cutoff:
                return r
        except ValueError:
            return r
    return None


def add_app(cfg: Config, company: str, position: str = "", url: str = "",
            status: str = "已投递", note: str = "", source: str = "扩展",
            applied_at: str = "") -> Tuple[Dict[str, Any], bool]:
    """新增一条投递记录。返回 (记录, 是否判为重复)。"""
    company = (company or "").strip()
    if not company:
        raise ValueError("公司名称不能为空")

    def _do(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
        dup = find_dup(rows, company, position, url)
        if dup:
            return dup, True
        rec = {
            "id": uuid.uuid4().hex[:8],
            "公司名称": company,
            "岗位名称": position.strip(),
            "状态": norm_status(status),
            "投递时间": (applied_at or now_str()).strip(),
            "网页链接": url.strip(),
            "来源": source.strip() or "扩展",
            "备注": note.strip(),
            "更新时间": now_str(),
        }
        rows.append(rec)
        return rec, False

    return mutate_apps(cfg, _do)


def update_app(cfg: Config, app_id: str, **fields) -> Optional[Dict[str, Any]]:
    def _do(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        hit = None
        old_c = old_p = ""
        for r in rows:
            if r.get("id") == app_id:
                old_c, old_p = r.get("公司名称", ""), r.get("岗位名称", "")
                for k, v in fields.items():
                    if k in CSV_FIELDS and v is not None:
                        r[k] = norm_status(v) if k == "状态" else str(v).strip()
                r["更新时间"] = now_str()
                hit = r
                break
        # 改了公司名 / 岗位名就把旧名字记下来 —— 同步那边要靠它认出是同一条（见 note_app_rename）
        if hit and (("公司名称" in fields and hit["公司名称"] != old_c)
                    or ("岗位名称" in fields and hit["岗位名称"] != old_p)):
            note_app_rename(cfg, app_id, rows, old_c, old_p)
        return hit

    return mutate_apps(cfg, _do)


def delete_app(cfg: Config, app_id: str) -> bool:
    def _do(rows: List[Dict[str, Any]]) -> bool:
        left = [r for r in rows if r.get("id") != app_id]
        if len(left) == len(rows):
            return False
        rows[:] = left
        note_app_rename(cfg, "", rows)     # 顺手清掉这条的曾用名，别让备忘文件越攒越胖
        return True

    return mutate_apps(cfg, _do)


# ---------------------------------------------------------------------------
# 「这条记录以前叫什么」备忘 —— 表格里改了公司名 / 岗位名之后，飞书同步还得认出是同一条
# ---------------------------------------------------------------------------
# 2026-10-04 加的。她要求表格里每一格都能改，但 merge_apps 是**按「公司名 + 岗位名」认人**
# 的：她改完名字，下一次同步就认不出本地这条了 —— 飞书表里那条还挂着旧名字，一比对落空，
# 就被当成新记录再插一条，同一家公司在她眼前变成两条（而且两条都长得挺像）。
# 所以改名时把旧名字记下来（只往上加、最多留 10 个），同步时连曾用名一起比。
# 存本地 data/app_renames.json，飞书同步不覆盖；她自己改回去只是「多了一个曾用名」，无副作用。
APP_RENAME_MAX = 10


def app_rename_path(cfg: Config) -> str:
    return os.path.join(cfg.out_dir, "app_renames.json")


def load_app_renames(cfg: Config) -> Dict[str, Dict[str, List[str]]]:
    """读曾用名。文件坏了当没有 —— 看板不能因为一个坏文件打不开。"""
    path = app_rename_path(cfg)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, Dict[str, List[str]]] = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        rec: Dict[str, List[str]] = {}
        for col in ("公司", "岗位"):
            vals = v.get(col)
            if not isinstance(vals, (list, tuple)):
                continue
            clean = [str(x).strip()[:120] for x in vals if str(x or "").strip()]
            if clean:
                rec[col] = clean[:APP_RENAME_MAX]
        if rec:
            out[k] = rec
    return out


def note_app_rename(cfg: Config, app_id: str, rows: List[Dict[str, Any]],
                    old_company: str = "", old_position: str = "") -> None:
    """记下改名前的旧名字；顺手清掉已经不存在的记录的曾用名。app_id 传空 = 只做清理。"""
    path = app_rename_path(cfg)
    if not app_id and not os.path.exists(path):
        return                                  # 没改过名、也没有这个文件 → 不白建一个
    with APP_FILE_LOCK:                         # 跟 applications.csv 同一把锁（RLock，可从临界区里再拿）
        obj = load_app_renames(cfg)
        live = {str(r.get("id") or "") for r in rows}
        obj = {k: v for k, v in obj.items() if k in live}
        if app_id:
            rec = obj.setdefault(app_id, {})
            for col, val in (("公司", old_company), ("岗位", old_position)):
                val = str(val or "").strip()[:120]
                if not val:
                    continue
                lst = rec.setdefault(col, [])
                if val not in lst:
                    lst.append(val)
                del lst[APP_RENAME_MAX:]
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 子公司 → 主公司（表里那个「并入」）
# ---------------------------------------------------------------------------
# 她要的：示例五局 / 示例南洋 / 示例国际城市开发 这几行，收在「示例建筑集团」一行下面。
# **程序不猜谁是子公司** —— 「中国」开头的大公司一堆（四大行、邮储、兵器、航发这类互不相干的），
# 按前缀一并就全糊成一家；「示例家居」（男装）跟「示例家居集团」（卫浴）更是长得比真母子还像。
# 所以只认她在页面上点过的那一条，存本地 data/app_parents.json（飞书同步不覆盖）。
#
# 键用**公司名归一后的那个键**（页面 coNorm 的结果，跟「归到一块」用的是同一个），
# 值是她指定/输入的主公司名；**空串是有意义的值** = 明确「不并了」，用来盖掉下面那份打底表。
APP_PARENT_FILE = "app_parents.json"
APP_PARENT_MAX = 60        # 名字截断长度，防手滑粘进来一整段
# 打底：几乎不可能认错的一条，让这个功能开箱就有个样子（她在页面上随时能「拆出」）
APP_PARENT_BUILTIN = {"TCL实业": "TCL"}


def app_parent_path(cfg: Config) -> str:
    return os.path.join(cfg.out_dir, APP_PARENT_FILE)


def load_app_parents(cfg: Config) -> Dict[str, str]:
    """读「并入」表。文件坏了当没有 —— 看板不能因为一个坏文件打不开。"""
    path = app_parent_path(cfg)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        k = k.strip()[:APP_PARENT_MAX]
        if not k:
            continue
        out[k] = v.strip()[:APP_PARENT_MAX]
    return out


def set_app_parent(cfg: Config, child: str, parent: str) -> Dict[str, str]:
    """记一条「这家并入那家」；parent 给空串 = 不并了（连打底表一起盖掉）。"""
    child = str(child or "").strip()[:APP_PARENT_MAX]
    parent = str(parent or "").strip()[:APP_PARENT_MAX]
    with APP_FILE_LOCK:                         # 跟投递记录同一把锁，避免并发写互相盖
        obj = load_app_parents(cfg)
        if parent and parent == child:
            obj.pop(child, None)          # 并到自己身上 = 等于没并，别留一条噪音
        else:
            obj[child] = parent
        path = app_parent_path(cfg)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    return obj


# ---------------------------------------------------------------------------
# 用已有记录反推「这个链接是哪家公司」
#
# 扩展抓公司名只能靠猜（标题、logo、URL），在招聘 SPA 上经常猜错
# （比如把「2027 Yunxi全球管理培训生计划」当成公司名）。但看板里本来就有
# 一百多条真实投递记录，链接和公司名是她自己填对的 —— 拿新链接去比对
# 老记录，比对出来的名字比猜准得多。
# ---------------------------------------------------------------------------

_URL_ID_SEG = re.compile(r"(?:\d+|[0-9a-f]{12,})")
_URL_FILE_EXT = re.compile(r"\.(?:html?|php|jsp|aspx?|shtml|do|action)$", re.I)
# 招聘平台自己的通用路径段：光靠这些段吻合，不能说明是同一家公司
_URL_GENERIC_SEG = {
    "campus", "campus-recruitment", "campusrecruitment", "recruitment", "recruit",
    "jobs", "job", "jobinfo", "jobdetail", "jobsdetail", "joblist", "position",
    "positions", "post", "posts", "apply", "applylist", "application", "applications",
    "index", "home", "main", "static", "spa", "custom", "list", "search", "detail",
    "info", "portal", "talent", "careers", "career", "web", "page", "candidatehome",
}

# 查询串里能说明「这是哪家公司」的参数名（各招聘平台叫法不一）。
# 故意不收 aid / resumeid / userid 这类 —— 那是「谁投的」，同一个人投谁家都一样，
# 拿它比对会把所有公司判成同一家。
_URL_TENANT_KEYS = {
    "ctmid", "companyid", "company_id", "compid", "companyno", "deptid", "dept_id",
    "tenant", "tenantid", "tenant_id", "organizationid", "orgid", "org_id",
    "firmid", "employerid", "enterpriseid", "corpid", "unitid", "cooperateid", "customerid",
}


def url_tenant(url: str) -> str:
    """取链接里代表「哪家公司」的那个参数值（应届生系统是 ctmid，别家叫法不同）。

    返回归一后的值（小写、只留字母数字），取不到就没值。
    """
    try:
        q = parse_qs(urlsplit((url or "").strip()).query, keep_blank_values=False)
    except ValueError:
        return ""
    for k, vs in q.items():
        if k.lower() not in _URL_TENANT_KEYS:
            continue
        v = re.sub(r"[^a-z0-9]", "", (vs[0] if vs else "").strip().lower())
        if len(v) >= 6:          # 太短的多半是页码 / 开关，不是公司 id
            return v
    return ""


def url_signature(url: str) -> Tuple[str, List[str]]:
    """把投递链接归一成 (主机, 路径段)。

    去掉查询串和 fragment（mokahr 这类 SPA 把职位 id 放在 # 后面，每次都不同），
    遇到纯数字 / 长 hash 的段就停 —— 那后面基本都是单条职位的 id。
    """
    try:
        p = urlsplit((url or "").strip())
    except ValueError:
        return "", []
    host = (p.netloc or "").lower()
    if not host:
        return "", []
    if host.startswith("www."):
        host = host[4:]
    segs: List[str] = []
    for raw in p.path.split("/"):
        s = unquote(raw).strip().lower()
        if not s or s in (".", ".."):
            continue
        s = _URL_FILE_EXT.sub("", s)
        if not s:
            continue
        if _URL_ID_SEG.fullmatch(s):
            break
        segs.append(s)
    return host, segs


def url_matches(a: str, b: str) -> int:
    """两条链接指向同一家公司时的吻合度（共同路径段数）。0 = 不像同一家。

    除了路径，还比一样东西：链接里带的那串「公司标识参数」。应届生求职网
    这类托管平台，同一家公司的岗位页和投递成功页路径完全不同
    （/xyzjob vs /xyzsuccess），但 ctmid 一模一样 —— 认这个比认路径靠谱。
    """
    ha, sa = url_signature(a)
    hb, sb = url_signature(b)
    if not ha or ha != hb:
        return 0
    ta, tb = url_tenant(a), url_tenant(b)
    n = 0
    for x, y in zip(sa, sb):
        if x != y:
            break
        n += 1
    # 公司标识参数一样 → 就是同一家，哪怕路径毫无共同点
    if ta and ta == tb:
        return max(n, 2)
    # 两边都带、但不一样 → 同一平台同一路径挂着很多家，不是同一家公司
    if ta and tb and ta != tb:
        return 0
    if not n:
        return 0
    # 共同段全是平台通用段（/campus/jobs 这种每家都一样）时，得多吻合一段才认
    if n < 2 and all(s in _URL_GENERIC_SEG for s in sa[:n]):
        return 0
    return n


def _guess_by_host(rows: List[Dict[str, Any]], target: str) -> Dict[str, Any]:
    """兜底：这台网站你以前只记过一家公司 —— 给个参考值，但不标「确信」。

    聚合站（应届生求职网、51job 那种挂着很多家的）一旦出现过两家以上，这里自动失效。
    """
    host, _ = url_signature(target)
    if not host:
        return {}
    seen: Dict[str, set] = {}
    for r in rows:
        h, _s = url_signature(r.get("网页链接") or "")
        c = (r.get("公司名称") or "").strip()
        if h and c:
            seen.setdefault(h, set()).add(c)
    got = seen.get(host) or set()
    if len(got) != 1:
        return {}
    return {"company": sorted(got)[0], "matched": "host", "confident": False,
            "evidence": "这台网站你以前只记过这一家公司"}


def _co_key(s: str) -> str:
    """公司名 / 页面文字的比对键：去掉所有空白，压成小写。

    招聘页上「长河 发展银行」「长河发展银行 」都算同名，比对前先归一。
    """
    return re.sub(r"[\s\u3000\u00a0]+", "", str(s or "")).lower()


def pick_company_hit(text: str, names: List[str]) -> Dict[str, Any]:
    """在一段页面文字里，挑出她本来就认识的公司名。

    纯函数（不碰任何文件），所以自检能拿合成数据把它钉住。

    为什么要拿她自己的名单当字典：招聘站上「公司名该从哪读」根本没有通用答案 ——
    标题常是项目名、logo 有时是 svg、信息栏各家叫法不同。但她自己的名单是**确定**的：
    投递记录、待投递、机会库里出现过的那几千个名字，页面上只要出现其中一个，
    那几乎就是它。这比"猜页面结构"可靠得多。

    返回 hit（最可能的那家，判不准就给空串）、sure（能不能直接替她填上）、
    cands（备选，给她点）。命中不了就都空着，不硬编一个。
    """
    key = _co_key(text)
    out: Dict[str, Any] = {"hit": "", "sure": False, "cands": [], "n": 0}
    if not key or not names:
        return out
    seen: Dict[str, int] = {}
    for raw in names:
        name = str(raw or "").strip()
        k = _co_key(name)
        if len(k) < 2:          # 一个字的名字没有区分度（「中」「海」到处都是）
            continue
        if k in seen:
            continue
        c = key.count(k)
        if c:
            seen[name] = c
    if not seen:
        return out
    # 长的赢：命中的名字互为子串时（长河发展银行 vs 发展银行），长的那个更具体。
    # 长度一样时看出现次数 —— 满页都是它，基本就是主角。
    ranked = sorted(seen.items(), key=lambda kv: (-len(_co_key(kv[0])), -kv[1], kv[0]))
    out["cands"] = [n for n, _c in ranked[:8]]
    out["n"] = len(ranked)
    top = ranked[0][0]
    kl = len(_co_key(top))
    # 只有两个字的名字太容易是巧合（「美团」可能只是页脚的合作方），
    # 除非整页就只命中这一个，否则不替她填、只放进备选让她点。
    if kl >= 4 or len(ranked) == 1:
        out["hit"] = top
        out["sure"] = True
    return out


def known_companies(cfg: Config) -> Tuple[List[str], List[str]]:
    """返回 (认名用的全量字典, 输入框自动补全用的近况名单)。

    认名用全量（含机会库那几千条 —— 有些公司她只收藏过、没真投过，不吃全量就认不出）；
    自动补全只给投递记录 + 待投递 —— 几千条塞进下拉框反而没法用。
    """
    full: List[str] = []
    recent: List[str] = []
    try:
        apps = load_apps(cfg)
    except Exception:
        apps = []
    for r in apps:
        c = str(r.get("公司名称") or "").strip()
        if c:
            recent.append(c)
    try:
        if cfg.pending_csv and os.path.exists(cfg.pending_csv):
            with open(cfg.pending_csv, "r", encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    c = str(r.get("公司名称") or "").strip()
                    if c:
                        recent.append(c)
    except Exception:
        pass
    full.extend(recent)
    try:
        rows, _synced = load_opportunities(cfg)
        for r in rows:
            c = str(r.get("company") or "").strip()
            if c:
                full.append(c)
    except Exception:
        pass
    # 近况名单新的排前面（自动补全先跳最近打交道的公司），但同一个名字只留一份
    dedup: List[str] = []
    seenk = set()
    for c in reversed(recent):
        k = _co_key(c)
        if k and k not in seenk:
            seenk.add(k)
            dedup.append(c)
    return full, dedup


def match_companies(cfg: Config, text: str) -> Dict[str, Any]:
    """页面文字 → 她认识的公司名。给扩展弹卡前的预填用。"""
    full, recent = known_companies(cfg)
    res = pick_company_hit(text, full)
    # 备选里优先保留"她真投过/收藏过"的那几家 —— 那些最可能是这次投的
    rk = {_co_key(c) for c in recent}
    res["cands"] = sorted(res["cands"], key=lambda c: (0 if _co_key(c) in rk else 1,
                                                      -len(_co_key(c))))
    res["known"] = recent[:400]
    return res


def guess_from_url(cfg: Config, url: str) -> Dict[str, Any]:
    """拿投递链接去老记录里找同一条/同一家，反推公司名与岗位名。

    matched: exact（同一条链接）/ prefix（同一家公司下的页面）/ ambiguous / host / none
    confident 为真时扩展可以直接拿来用；否则只当参考，让人自己核。
    """
    out: Dict[str, Any] = {"company": "", "position": "", "matched": "none",
                           "confident": False, "evidence": ""}
    target = (url or "").strip()
    if not target or not target.lower().startswith(("http://", "https://")):
        return out
    rows = load_apps(cfg)
    if not rows:
        return out

    hits: List[Tuple[int, Dict[str, Any]]] = []
    for r in rows:
        u = (r.get("网页链接") or "").strip()
        if not u:
            continue
        if u == target:
            hits.append((10000, r))
            continue
        n = url_matches(target, u)
        if n:
            hits.append((n, r))
    if not hits:
        return dict(out, **_guess_by_host(rows, target))

    hits.sort(key=lambda h: -h[0])
    best = hits[0][0]
    top = [r for s, r in hits if s == best]
    names: List[str] = []
    for r in top:
        c = (r.get("公司名称") or "").strip()
        if c and c not in names:
            names.append(c)
    if len(names) != 1:
        # 同一段路径下挂着好几家公司，不敢下结论
        out.update(company="", matched="ambiguous",
                   evidence="; ".join(n for n in names[:3]))
        return out

    exact = best >= 10000
    rec = top[0]
    out["company"] = names[0]
    out["position"] = (rec.get("岗位名称") or "").strip() if exact else ""
    out["matched"] = "exact" if exact else "prefix"
    out["confident"] = exact or best >= 2
    out["evidence"] = (rec.get("网页链接") or "")[:200]
    return out


# ---------------------------------------------------------------------------
# 从飞书表格导入历史投递记录（一次性，可重复跑）
# ---------------------------------------------------------------------------
#
# 很多人是先拿飞书表格记投递的，表里状态是自己随口写的（"已测评""待一面"…），
# 跟看板这 6 个标准状态对不上。所以导入时按下面这张表翻译，
# 翻不出来的一律当「已投递」（宁松不严：宁可让它出现在进度里，也不要悄悄丢一条）。

APP_STATUS_MAP = {
    # 还停在"投了/等消息"
    "已投递": "已投递", "已申请": "已投递", "已网申": "已投递", "投递成功": "已投递",
    "待简历": "已投递", "简历筛选中": "已投递",
    # 过了初筛 / 进了测评笔试环节
    "简历通过": "简历通过",
    "已测评": "测评笔试", "待测评": "测评笔试", "测评": "测评笔试", "笔试": "测评笔试",
    "已笔试": "测评笔试", "待笔试": "测评笔试", "在线测评": "测评笔试",
    # 到了面试环节（AI 面也算）
    "已AI面": "面试", "AI面": "面试", "待AI面": "面试",
    "待一面": "面试", "已一面": "面试", "一面": "面试", "待二面": "面试",
    "已二面": "面试", "二面": "面试", "待HR面": "面试", "已HR面": "面试",
    "HR面": "面试", "终面": "面试", "待终面": "面试", "已终面": "面试",
    "已约面": "面试", "面试": "面试",
    # 结束了
    "已挂": "已结束", "挂了": "已结束", "感谢信": "已结束", "已拒": "已结束",
    "流程结束": "已结束", "已结束": "已结束",
    # 拿到 offer（飞书表那列是选项，她自己加的 "OC!" 就是 offer 的意思）
    "Offer": "Offer", "offer": "Offer", "已offer": "Offer", "已Offer": "Offer",
    "录用": "Offer", "已录用": "Offer", "意向书": "Offer", "已意向": "Offer",
    "OC!": "Offer", "OC": "Offer", "oc!": "Offer", "已OC": "Offer",
    # 自己放弃的 —— 流程也确实结束了（放"已结束"里，跟挂掉一样不再占进行中）
    "已放弃": "已结束", "放弃": "已结束",
}
# 这些是「收藏了但还没投」——不该混进投递进度（会把总数和"我投过的公司"算多），
# 单独存一份待投清单。
APP_NOT_APPLIED = {"未投递", "待投递", "想投", "收藏", "意向岗位"}

# 飞书表里那些「某一轮的日期」字段，值往往是「9月12日 23:59」这种自由文本，
# 统一拼进备注，看板原来的 CSV 没有这些列。
APP_ROUND_FIELDS = ["笔试", "AI面", "一面", "二面", "HR面"]

APP_IMPORT_HEAD_WARN = "看板投递记录只认这些列"

_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*(https?://[^\s)]+)\s*\)")


def app_pick_url(s: str) -> str:
    """飞书表里的链接常写成 markdown 的 `[标题](url)`，这里只取 URL。

    那一列是文本列，有人会直接写岗位名进去 —— 不是 http 开头的就别当链接，
    否则页面上会渲染出一个 `href="项目管理"` 的死链。
    """
    s = (s or "").strip()
    if not s:
        return ""
    m = _MD_LINK_RE.search(s)
    u = (m.group(2) if m else s).strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


def _md_title(s: str) -> str:
    """取 markdown 链接的标题；标题本身就是 URL 的话不算标题。"""
    m = _MD_LINK_RE.search((s or "").strip())
    if not m:
        return ""
    t = m.group(1).strip()
    return "" if t.lower().startswith("http") else t


def app_pick_time(s: str) -> str:
    """飞书的日期可能是 ISO（`2026-09-04T00:00:00.000+08:00`），也可能是毫秒时间戳。

    统一成看板用的 `YYYY-MM-DD HH:MM`；认不出来就返回空（宁可显示"—"也不要写错的日期）。
    """
    v = _clean_dt(s)
    return v if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", v) else ""


def app_map_status(s: str) -> str:
    """把飞书表里五花八门的状态翻成看板的 6 个标准状态。"""
    s = (s or "").strip()
    if s in APP_STATUS_MAP:
        return APP_STATUS_MAP[s]
    key = s.lower()
    for alias, real in APP_STATUS_MAP.items():
        if alias.lower() == key:
            return real
    # 没见过的写法别猜死：能对上几个关键字就归过去，否则保守当「已投递」
    if any(w in s for w in ("挂", "拒", "感谢信", "结束", "放弃")):
        return "已结束"
    if "面" in s:
        return "面试"
    if any(w in s for w in ("测评", "笔试")):
        return "测评笔试"
    if "offer" in s.lower() or "录用" in s or s.upper().startswith("OC"):
        return "Offer"
    return "已投递"


def app_pick_round(s: str) -> str:
    """各轮日期列：可能是 ISO，也可能是「9月12日 23:59」这种手写文本。"""
    s = (s or "").strip()
    if not s:
        return ""
    t = app_pick_time(s)
    return t[:10] if t else s


def app_build_note(raw_note: str, rounds: Dict[str, str], refer: str) -> str:
    """备注 = 原备注 + 各轮日期 + 内推码，太长就截断（表格那一列放不下）。"""
    parts = []
    if (raw_note or "").strip():
        parts.append(raw_note.strip())
    for label in APP_ROUND_FIELDS:
        v = app_pick_round(rounds.get(label))
        if v:
            parts.append("%s %s" % (label, v))
    if (refer or "").strip():
        parts.append("内推码 " + refer.strip())
    out = " · ".join(parts)
    return out if len(out) <= 80 else out[:79] + "…"


def fetch_apps_from_feishu(cfg: Config) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """拉飞书投递表，返回 (要导进看板的记录, 还没投的岗位清单)。

    各家表结构不一样，所以只认「公司名称」这一列必须有；其余字段有就用、没有就跳过。
    """
    cli = _find_lark_cli()
    if not cli:
        raise RuntimeError("没找到 lark-cli（飞书命令行）。装法见 README")
    if not (cfg.app_base and cfg.app_table):
        raise RuntimeError("config.ini 里没配 [applications] 的 feishu_base_token / feishu_table_id")
    env = _lark_env(cli)   # 顺手摘掉代理变量，见 _lark_env 的注释

    rows: List[Dict[str, Any]] = []
    offset = 0
    for _ in range(10):   # 一页 200 条，最多 2000 条，投递记录够用了
        args = [cli, "base", "+record-list",
                "--base-token", cfg.app_base, "--table-id", cfg.app_table,
                "--page-size", "200", "--offset", str(offset),
                "--format", "json", "--as", "user"]
        if cfg.app_profile:
            args[1:1] = ["--profile", cfg.app_profile]
        try:
            pr = subprocess.run(args, capture_output=True, timeout=90, env=env, cwd=cfg.base)
        except subprocess.TimeoutExpired:
            raise RuntimeError("调 lark-cli 超时（90 秒）")
        except OSError as e:
            raise RuntimeError("调 lark-cli 失败：%s" % e)
        if pr.returncode != 0:
            msg = (pr.stderr or pr.stdout or b"").decode("utf-8", "replace").strip()
            raise RuntimeError("lark-cli 退出码 %s：%s" % (pr.returncode, msg[:300]))
        try:
            obj = json.loads(pr.stdout.decode("utf-8", "replace"))
        except ValueError:
            raise RuntimeError("lark-cli 返回的不是 JSON")
        if not obj.get("ok", True):
            raise RuntimeError("飞书返回 ok=false：%s" % str(obj.get("error", ""))[:200])

        data = obj.get("data") or {}
        if not (data.get("fields") or []):
            break
        rows.extend(parse_feishu_apps(obj))

        if not data.get("has_more"):
            break
        page = data.get("data") or []
        offset += len(page)
        if not page:
            break

    return split_pending(rows)


def split_pending(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按状态把记录分成「已投的」和「还没投的」。"""
    applied = [r for r in rows if r["状态"] != "未投递"]
    pending = [r for r in rows if r["状态"] == "未投递"]
    return applied, pending


def parse_feishu_apps(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 lark-cli 返回的矩阵（fields 列名 + data 行数组）转成待导入记录。

    纯函数，不碰网络，方便自检直接喂样例 JSON。
    """
    data = obj.get("data") or {}
    cols = data.get("fields") or []
    page = data.get("data") or []
    if not cols:
        return []
    idx = {c: i for i, c in enumerate(cols)}

    def g(row, name):
        i = idx.get(name)
        return _cell_str(row[i]) if (i is not None and i < len(row)) else ""

    out: List[Dict[str, Any]] = []
    for row in page:
        company = g(row, "公司名称").strip()
        position = g(row, "岗位名称").strip()
        raw_link = g(row, "投递链接").strip()
        st_raw = g(row, "最新状态").strip()
        # 整行都没东西 —— 表里的空占位行，跳过
        if not (company or position or raw_link or st_raw):
            continue
        note_head = ""
        if not company:
            # 表里确实有这种行：公司名空着，只写了岗位和链接。
            # 不猜（猜错的公司名会串到机会库的「我投过的公司」匹配里去），
            # 用占位名 + 把链接标题带上，让她在看板上一眼看到该补哪条。
            company = "未填公司"
            title = _md_title(raw_link)
            note_head = "原表没填公司名" + ("（链接标题：%s）" % title[:40] if title else "")
        rounds = {k: g(row, k) for k in APP_ROUND_FIELDS}
        note = app_build_note(g(row, "备注"), rounds, g(row, "内推码"))
        out.append({
            "公司名称": company,
            "岗位名称": position,
            "状态": "未投递" if st_raw in APP_NOT_APPLIED else app_map_status(st_raw),
            "投递时间": app_pick_time(g(row, "投递时间")),
            "网页链接": app_pick_url(raw_link),
            "备注": (note_head + " · " + note).strip(" ·"),
            "_原状态": st_raw,
            "_地点": g(row, "地点").strip(),
        })
    return out


def import_apps(cfg: Config, dry_run: bool = False,
                rows: Optional[List[Dict[str, Any]]] = None) -> Tuple[int, int, int, List[Dict[str, Any]]]:
    """把飞书表里的历史投递记录合并进本地 CSV。

    幂等：同公司 + 同岗位已经在了就跳过 —— 不重复导入，也不覆盖你在看板上改过的状态。
    返回 (新增, 跳过, 待投数, 待投清单)。传 rows 可以跳过网络（自检用）。
    写盘跟别处共用 mutate_apps（同一把锁 + 同一份保列逻辑），不再自己 _write_all。
    """
    if rows is None:
        applied, pending = fetch_apps_from_feishu(cfg)
    else:
        applied, pending = split_pending(rows)

    def _plan(old: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        seen = {(r.get("公司名称", "").strip(), (r.get("岗位名称") or "").strip()) for r in old}
        added: List[Dict[str, Any]] = []
        skipped = 0
        for r in applied:
            key = (r["公司名称"], r["岗位名称"])
            if key in seen:
                skipped += 1
                continue
            seen.add(key)
            added.append({
                "id": uuid.uuid4().hex[:8],
                "公司名称": r["公司名称"],
                "岗位名称": r["岗位名称"],
                "状态": r["状态"],
                # 原表没写投递时间的就空着 —— 页面显示「—」、排在最后。
                # 别拿导入那一刻顶上去：那会让「今天投了几家」这些数凭空多出来。
                "投递时间": r["投递时间"],
                "网页链接": r["网页链接"],
                "来源": "飞书表",
                "备注": r["备注"],
                "更新时间": now_str(),
            })
        # 同一天投的按原表顺序排在一起，整体还是按投递时间升序（页面显示时会倒过来）
        added.sort(key=lambda r: r["投递时间"])
        return added, skipped

    if dry_run:
        added, skipped = _plan(load_apps(cfg))
        return len(added), skipped, len(pending), pending

    def _do(old: List[Dict[str, Any]]) -> Tuple[int, int]:
        added, skipped = _plan(old)
        old.extend(added)
        return len(added), skipped

    n_added, n_skipped = mutate_apps(cfg, _do)
    if pending:
        _write_pending(cfg, pending)
    return n_added, n_skipped, len(pending), pending


def _write_pending(cfg: Config, rows: List[Dict[str, Any]]) -> None:
    """「收藏了但还没投」的岗位单独存一份，不混进投递进度。"""
    os.makedirs(os.path.dirname(os.path.abspath(cfg.pending_csv)), exist_ok=True)
    tmp = cfg.pending_csv + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "id": uuid.uuid4().hex[:8],
                "公司名称": r["公司名称"],
                "岗位名称": r["岗位名称"],
                "状态": "未投递",
                "投递时间": "",
                "网页链接": r["网页链接"],
                "来源": "飞书表",
                "备注": (r["备注"] + (" · " + r["_地点"] if r.get("_地点") else "")).strip(" ·"),
                "更新时间": now_str(),
            })
    os.replace(tmp, cfg.pending_csv)


# ---------------------------------------------------------------------------
# 投递进度：跟飞书那张表持续同步（表里改了状态，看板跟着变）
# ---------------------------------------------------------------------------
#
# 她是拿飞书表格记投递进度的：投完改一次状态、进面了再改一次。看板要是一直只认
# 导入那天的快照，之后就永远是旧状态 —— 所以这里做**持续同步**，而不是一次性导入。
#
# 冲突规则（同一条两边都可能被改）：
#   把飞书那一格的**原文**抄进本地「飞书状态」列，下次同步对比就知道谁动过：
#     · 飞书状态 ≠ 上次抄下来的  → 飞书那边动过 → 以飞书为准（状态/时间/备注一起跟上）
#     · 飞书状态还一样          → 你在看板上手改的状态保留，不会被按回飞书的老值
#   飞书那边没动、本地也没动 → 只更新「已同步过」的时间，不刷记录的更新时间。

APP_RETRY_AFTER = 600           # 同步失败后隔多久允许重试（秒）
APP_STATE: Dict[str, Any] = {"at": 0.0, "ok": None, "msg": "还没同步过", "busy": False,
                             "count": 0, "added": 0, "updated": 0, "pending": 0}
APP_LOCK = threading.Lock()


def _app_exact_key(company: str, position: str) -> Tuple[str, str]:
    return ((company or "").strip(), (position or "").strip())


def _app_clean_position(s: str) -> str:
    return re.sub(r"[\s·・\-—_()（）\[\]【】／/]+", "", (s or "")).lower()


def _app_loose_key(company: str, position: str) -> Tuple[str, str]:
    """宽松一点：公司名去掉「有限公司」这类后缀，岗位名去掉空格标点。"""
    return (norm_company(company), _app_clean_position(position))


def _short_err(e: Any) -> str:
    """把 lark-cli 那一坨 JSON 报错压成一行 —— 页面上那行字是给人看的，不是给日志看的。"""
    s = re.sub(r"\s+", " ", str(e)).strip()
    # message 里常嵌着转义引号（`Get \"https://…\" failed`），所以不能简单地 [^"]+
    m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.){1,300})"', s)
    if m:
        s = re.sub(r"\\(.)", r"\1", m.group(1)).strip()
    return s if len(s) <= 130 else s[:129] + "…"


def merge_apps(local: List[Dict[str, Any]], applied: List[Dict[str, Any]],
               now: str = "", renames: Optional[Dict[str, Dict[str, List[str]]]] = None,
               ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """把飞书表里的投递记录合并进本地记录（纯函数：不碰磁盘、不联网）。

    飞书表里没出现的本地记录（扩展记的、你手动加的）**原样留着** —— 同步只往前推进，
    不会因为你还没往飞书表里补一条就把它从看板上抹掉。

    renames = 本地的「曾用名」备忘（她在看板上改过公司名 / 岗位名的那种记录）：
    同步时先把现名索引一遍，再把曾用名补进去 —— 这样飞书那条（还挂着旧名字）照样能对上，
    不会因为改过名就在看板上多出一条重复记录。
    """
    now = now or now_str()
    rows = [dict(r) for r in local]
    by_exact: Dict[tuple, int] = {}
    by_loose: Dict[tuple, int] = {}
    by_url: Dict[str, int] = {}
    for i, r in enumerate(rows):
        by_exact.setdefault(_app_exact_key(r.get("公司名称", ""), r.get("岗位名称", "")), i)
        by_loose.setdefault(_app_loose_key(r.get("公司名称", ""), r.get("岗位名称", "")), i)
        u = (r.get("网页链接") or "").strip()
        if u:
            by_url.setdefault(u, i)
    # 第二遍才补曾用名：setdefault 不覆盖，所以「现名」永远赢过「曾用名」，
    # 不会出现「A 公司的旧名字占了 B 公司的位置」这种事
    for i, r in enumerate(rows):
        alt = (renames or {}).get(str(r.get("id") or "")) or {}
        if not alt:
            continue
        comps = [r.get("公司名称", "")] + list(alt.get("公司") or [])
        poss = [r.get("岗位名称", "")] + list(alt.get("岗位") or [])
        for c in comps:
            for p in poss:
                by_exact.setdefault(_app_exact_key(c, p), i)
                by_loose.setdefault(_app_loose_key(c, p), i)

    used = set()
    seen_new = set()
    stats: Dict[str, Any] = {"added": 0, "updated": 0, "unchanged": 0, "dup": 0, "changes": []}
    for r in applied:
        company = (r.get("公司名称") or "").strip()
        position = (r.get("岗位名称") or "").strip()
        url = (r.get("网页链接") or "").strip()
        key = _app_exact_key(company, position)

        idx = by_exact.get(key)
        if idx is None or idx in used:
            idx = None
            if url and by_url.get(url) is not None and by_url[url] not in used:
                idx = by_url[url]                    # 链接一样就是同一条（岗位名可能改过）
            else:
                lk = _app_loose_key(company, position)
                if by_loose.get(lk) is not None and by_loose[lk] not in used:
                    idx = by_loose[lk]

        if idx is None:
            if key in seen_new:                      # 飞书表里自己重复的行，只进一条
                stats["dup"] += 1
                continue
            seen_new.add(key)
            rows.append({
                "id": uuid.uuid4().hex[:8],
                "公司名称": company,
                "岗位名称": position,
                "状态": r["状态"],
                "飞书状态": (r.get("_原状态") or "").strip(),
                # 原表没写投递时间的就空着（页面显示「—」），别拿同步那一刻顶上去
                "投递时间": r["投递时间"],
                "网页链接": url,
                "来源": "飞书表",
                "备注": r["备注"],
                "更新时间": now,
            })
            stats["added"] += 1
            stats["changes"].append({"公司名称": company, "岗位名称": position,
                                     "diff": ["新记一条（%s）" % r["状态"]]})
            continue

        used.add(idx)
        row = rows[idx]
        raw = (r.get("_原状态") or "").strip()
        prev = (row.get("飞书状态") or "").strip()
        diffs: List[str] = []
        if raw != prev:                              # 飞书那边动过（或这条还没同步过基线）
            if row.get("状态") != r["状态"]:
                diffs.append("状态 %s → %s" % (row.get("状态") or "—", r["状态"]))
                row["状态"] = r["状态"]
            if r["投递时间"] and row.get("投递时间") != r["投递时间"]:
                diffs.append("投递时间 → %s" % r["投递时间"])
                row["投递时间"] = r["投递时间"]
            if r["备注"] and row.get("备注") != r["备注"]:
                diffs.append("备注跟着更新")
                row["备注"] = r["备注"]
            row["飞书状态"] = raw
        if not row.get("网页链接") and url:
            row["网页链接"] = url
        if diffs:
            row["更新时间"] = now
            stats["updated"] += 1
            stats["changes"].append({"公司名称": row.get("公司名称", ""),
                                     "岗位名称": row.get("岗位名称", ""), "diff": diffs})
        else:
            stats["unchanged"] += 1
    # 飞书表里没有、只在本地有的（扩展新记的 / 手动加的）—— 原样保留
    stats["local_only"] = len(local) - len(used)
    return rows, stats


def read_apps_state(cfg: Config) -> Dict[str, Any]:
    try:
        with open(cfg.apps_sync_json, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_apps_state(cfg: Config, state: Dict[str, Any]) -> None:
    """把上次同步时间落盘 —— 服务重启后不必白拉一次 lark-cli（一次 2~5 秒）。"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(cfg.apps_sync_json)), exist_ok=True)
        tmp = cfg.apps_sync_json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, cfg.apps_sync_json)
    except OSError:
        pass


def apps_seed_state(cfg: Config) -> None:
    if APP_STATE.get("at"):
        return
    d = read_apps_state(cfg)
    if not d.get("at"):
        return
    APP_STATE.update({"at": float(d.get("at") or 0), "ok": True,
                      "msg": str(d.get("msg") or ""), "count": int(d.get("count") or 0)})


def apps_ok_recently(cfg: Config) -> bool:
    at = APP_STATE.get("at") or 0.0
    return bool(at) and (time.time() - at) < cfg.apps_ttl


def sync_apps(cfg: Config, force: bool = False, quiet: bool = False,
              rows: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    """从飞书表同步一次投递进度。rows 传进来就跳过网络（自检用）。"""
    if rows is None and cfg.apps_source != "feishu":
        return True, "数据源不是飞书"
    if rows is None and not force and apps_ok_recently(cfg):
        return True, str(APP_STATE.get("msg") or "")
    if not APP_LOCK.acquire(blocking=False):
        return True, "正在同步"
    APP_STATE["busy"] = True
    try:
        if rows is None:
            # 飞书偶发 EOF / 连接重置（2026-09-29 一天碰上三四次），退避重试三次基本能过
            applied = pending = None
            last: Optional[Exception] = None
            for attempt, wait in ((1, 2), (2, 5), (3, 10)):
                try:
                    applied, pending = fetch_apps_from_feishu(cfg)
                    break
                except RuntimeError as e:
                    last = e
                    if wait:
                        time.sleep(wait)
            if applied is None:
                raise last if last else RuntimeError("拉取失败")
        else:
            applied, pending = split_pending(rows)
        # 同步的写入段也持 APP_FILE_LOCK（跟用户手改串起来）。加锁次序：先 APP_LOCK 再
        # APP_FILE_LOCK，本地写入只拿 APP_FILE_LOCK、从不反向要 APP_LOCK → 无环，不会死锁。
        with APP_FILE_LOCK:
            merged, stats = merge_apps(load_apps(cfg), applied, renames=load_app_renames(cfg))
            _write_all(cfg, merged)
            note_app_rename(cfg, "", merged)    # 同步会把飞书新记录插进来，顺手清一遍曾用名
        _write_pending(cfg, pending)
        APP_STATE.update({"at": time.time(), "ok": True, "count": len(merged),
                          "added": stats["added"], "updated": stats["updated"],
                          "pending": len(pending),
                          "msg": "已同步 %d 条（新增 %d / 更新 %d / 待投递 %d）"
                                 % (len(merged), stats["added"], stats["updated"], len(pending))})
        write_apps_state(cfg, {"at": APP_STATE["at"], "msg": APP_STATE["msg"],
                               "count": len(merged), "added": stats["added"],
                               "updated": stats["updated"], "at_str": now_str()})
        _APP_LAST_CHANGES[:] = stats["changes"][:50]
    except Exception as e:
        # 失败别按满 TTL 记，否则网络抖一下接下来半小时都不再重试
        retry = min(APP_RETRY_AFTER, max(60, cfg.apps_ttl // 2))
        msg = _short_err(e)
        APP_STATE.update({"at": time.time() - (cfg.apps_ttl - retry), "ok": False, "msg": msg})
        if not quiet:
            print("✗ 投递进度同步失败：%s" % msg)
        return False, msg
    finally:
        APP_STATE["busy"] = False
        APP_LOCK.release()
    if not quiet:
        print("✓ " + str(APP_STATE["msg"]) + "  → " + cfg.app_csv)
    return True, str(APP_STATE["msg"])


_APP_LAST_CHANGES: List[Dict[str, Any]] = []


def sync_apps_bg(cfg: Config, force: bool = False) -> None:
    """后台线程里同步，别卡住 /api/board 那次请求。已经在拉了就直接返回。"""
    if APP_STATE.get("busy"):
        return
    threading.Thread(target=sync_apps, kwargs={"force": force, "quiet": True},
                     args=(cfg,), daemon=True).start()


# ---------------------------------------------------------------------------
# 邮箱通知（读 qqmail2table 的 CSV）
# ---------------------------------------------------------------------------

def norm_company(s: str) -> str:
    """公司名归一：去掉括号里那截和一眼就能删的通用后缀，用来做模糊配对。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "")
    for suf in ("有限责任公司", "股份有限公司", "有限公司", "集团有限公司", "集团",
                "控股", "信息技术", "电子商务", "科技", "网络", "公司"):
        s = s.replace(suf, "")
    return re.sub(r"[\s·・\-—_]+", "", s).strip()


def same_company(a: str, b: str) -> bool:
    a, b = norm_company(a), norm_company(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 2 and len(b) >= 2 and (a in b or b in a)


def company_prefix_ok(short: str, long_: str) -> bool:
    """短名算不算长名的「同一个主体」。只给比对用，不对外。

    - 短名 <2 字：不算（单字撞名太多）。
    - 完全相等：算。
    - 短名 ≥3 字：算 —— 「长河银行」⊂「长河银行杭州分行」、「雀巢」⊂「雀巢GCR」。
    - 短名只有 2 字：**长名必须多出 ≥2 个字**才算。多出单字基本是撞名：
      「京东」+「方」= 京东方 —— 两家完全不一样（2026-10-09 她亲口纠正我按「笔误」猜）；
      而「泰康」+「人寿」/「建发」+「股份」/「达能」+「中国」都确实是同一家。
    """
    if short == long_:
        return True
    if short not in long_ or len(short) < 2:
        return False
    if len(short) >= 3:
        return True
    return len(long_) - len(short) >= 2


def same_company_tight(a: str, b: str) -> bool:
    """「这条投递挂哪些通知」（关联列）专用的公司比对，比共用件 same_company 严一档：
    子串包含要过 company_prefix_ok（短名 2 字时要求长名多出 ≥2 字）。

    为什么不直接改 same_company —— 那是**网页 + Edge 扩展共用**的，动它会牵动整张通知表
    （2026-10-09 的教训：邮件路径的收紧一律新增专用函数）。这里只影响看板的「关联」列，
    不改任何通知的判定、不改她的数据。分行照旧认得上（「长河银行」4 字 ⊂「长河银行杭州分行」）。
    """
    a, b = norm_company(a), norm_company(b)
    if not a or not b:
        return False
    lo, hi = sorted([a, b], key=len)
    return company_prefix_ok(lo, hi)


# ---------------------------------------------------------------------------
# 邮箱通知：数据源（飞书多维表格 / 现成 CSV）
# ---------------------------------------------------------------------------

# 看板读的 CSV 列。飞书同步写出来的就是这个格式，
# 比 qqmail2table 的产物多一个「处理状态」（飞书那边手工标的）
NOTICE_FIELDS = ["公司名称", "岗位名称", "通知类型", "通知截止时间",
                 "邮件链接", "邮件主题", "邮件收到时间", "处理状态"]

# 处理状态：飞书那边用前三个，看板上还能标「已忽略」（这条不用管了）。
# 空值按「待处理」算。
NOTICE_STATES = ["待处理", "已完成", "已过期", "已忽略"]
# 这几个状态意味着「这件事已经了结」，不再计入逾期/待办
NOTICE_CLOSED = ("已完成", "已过期", "已忽略")


def notice_state_key(r: Dict[str, Any]) -> str:
    """一条通知的稳定身份。飞书同步会重写 CSV，行号会变，
    所以用内容算哈希——同一条通知每次算出来的都一样。"""
    parts = [
        str(r.get("公司名称", "")).strip(),
        "、".join(r.get("通知类型") or []) if isinstance(r.get("通知类型"), list)
        else str(r.get("通知类型", "")).strip(),
        str(r.get("通知截止时间", "")).strip(),
        str(r.get("邮件主题", "")).strip(),
    ]
    raw = "\x1f".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def notice_states_path(cfg: "Config") -> str:
    return os.path.join(cfg.out_dir, "notice_states.json")


def load_notice_states(cfg: "Config") -> Dict[str, str]:
    """读本地手动标的状态。文件坏了当没有，不让看板打不开。"""
    path = notice_states_path(cfg)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in obj.items():
        s = v.get("状态") if isinstance(v, dict) else v
        if isinstance(k, str) and isinstance(s, str) and s in NOTICE_STATES:
            out[k] = s
    return out


def set_notice_state(cfg: "Config", key: str, status: str) -> bool:
    """写一条手动状态。status 传空 = 撤掉这条覆盖，回到飞书给的值。"""
    key = str(key or "").strip()
    status = str(status or "").strip()
    if not key:
        return False
    path = notice_states_path(cfg)
    obj: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                obj = loaded
        except (OSError, ValueError):
            obj = {}
    if status:
        if status not in NOTICE_STATES:
            return False
        obj[key] = {"状态": status, "时间": now_str()}
    else:
        obj.pop(key, None)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return True

_MD_LINK = re.compile(r"^\[([^\]]*)\]\(([^)]*)\)$")


def _rekey(old: str, new: str, path: str) -> None:
    """把一个 json 文件里某个键改名（值原样保留）。文件不存在 / 坏了 / 新旧撞车 → 什么都不做。

    搬家失败绝不能影响主流程 —— 最坏情况只是用户重标一次，不能让他连类型都改不了。
    """
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict) or old not in obj or new in obj:
            return
        obj[new] = obj.pop(old)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except (OSError, ValueError):
        return


def set_notice_type(cfg: "Config", key: str, ntype: str) -> bool:
    """给一条通知手填类型（ntype 传空 = 改回「待确认」）。返回有没有改到。

    为什么是**直接改写 notices.csv** 而不是再存一份旁路覆盖：类型拿不准的邮件现在已经
    作为正常一行进表了，再弄一份 override 就是两个地方各说一套（改了这边、那边没动）。
    ⚠️ key 是按「公司 + 类型 + 截止 + 主题」算的，**改了类型 key 就变了** ——
    所以挂在旧 key 上的「处理状态」和「识别依据」要一起搬过去，
    否则用户刚标完「已处理」，改一下类型那标记就悄悄没了。
    """
    key = str(key or "").strip()
    ntype = str(ntype or "").strip()
    if not key or not cfg.notices_csv:
        return False
    if ntype and ntype not in KNOWN_MAIL_TYPES:
        return False
    if not SYNC_LOCK.acquire(timeout=5):      # 别跟后台同步抢同一份 notices.csv
        return False
    try:
        rows = mail_existing_notice_rows(cfg)
        hit = None
        if os.path.exists(cfg.notices_csv):
            for r in rows:
                if notice_state_key(r) == key:
                    hit = r
                    break
        if hit is not None:
            hit["通知类型"] = [ntype] if ntype else []
            write_notices_csv(cfg, rows)
            new_key = notice_state_key(hit)
    except OSError:
        hit = None
    finally:
        SYNC_LOCK.release()
    if hit is not None:
        if new_key != key:
            _rekey(key, new_key, notice_states_path(cfg))
            _rekey(key, new_key, mail_evidence_path(cfg))
        return True
    # CSV 里找不到 → 去手动表里按 notice_state_key 匹配（手动通知的类型也要能改）
    mrows = load_manual_notices(cfg)
    mhit = None
    for r in mrows:
        if notice_state_key(r) == key:
            mhit = r
            break
    if mhit is None:
        return False
    mhit["通知类型"] = [ntype] if ntype else []
    new_key = notice_state_key(mhit)
    save_manual_notices(cfg, mrows)
    if new_key != key:
        _rekey(key, new_key, notice_states_path(cfg))
        _rekey(key, new_key, mail_evidence_path(cfg))
    return True
_ISO_DT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})")


def _find_lark_cli() -> str:
    """找本机的 lark-cli（飞书命令行工具）。找不到返回空串。"""
    p = shutil.which("lark-cli")
    if p:
        return p
    home = os.path.expanduser("~")
    cands = [os.path.join(home, ".workbuddy", "binaries", "node",
                          "cli-connector-packages", "bin", "lark-cli")]
    vroot = os.path.join(home, ".workbuddy", "binaries", "node", "versions")
    if os.path.isdir(vroot):
        for v in sorted(os.listdir(vroot)):
            cands.append(os.path.join(vroot, v, "bin", "lark-cli"))
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def _node_dir() -> str:
    """找一个**真的含 node 可执行文件**的目录，找不到返回空串。

    lark-cli 是个 `#!/usr/bin/env node` 的脚本，PATH 里没有 node 它就直接
    `env: node: No such file or directory`。平时在终端里跑没问题（登录 shell 的
    PATH 里有 node），但**launchd 起的服务 PATH 很干净**，只能靠 plist 里写死的那
    一行 —— 而那一行会随 node 升级过期（踩过一次：22.22.2-3 被换成 22.22.2-5，
    服务照常活着、页面照常能开，只有飞书同步从此静默失败，跑了 6 小时的定时同步
    一条也没进来）。所以这里自己兜一层，不再指望外部 PATH 写对。
    """
    p = shutil.which("node")
    if p:
        return os.path.dirname(p)
    home = os.path.expanduser("~")
    vroot = os.path.join(home, ".workbuddy", "binaries", "node", "versions")
    if os.path.isdir(vroot):
        for v in sorted(os.listdir(vroot), reverse=True):   # 新版本优先
            c = os.path.join(vroot, v, "bin", "node")
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return os.path.dirname(c)
    for c in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        if os.path.isfile(os.path.join(c, "node")):
            return c
    return ""


def _lark_env(cli: str) -> Dict[str, str]:
    """调 lark-cli 用的环境变量。

    **必须把代理变量摘掉**：这台机器上有 WorkBuddy 注入的本地透明代理，它会把
    `*.feishu.cn` 的 TLS 握手掐掉 —— 症状是 lark-cli 报
    `network / transport`、`EOF`，而别的域名（larksuite / qq / baidu）都正常。
    2026-09-29 实测：裸重试 31 次全败，`env -u HTTP_PROXY …` 一次就通。
    """
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    paths = [os.path.dirname(cli)]
    nd = _node_dir()
    if nd:
        paths.append(nd)                 # 见 _node_dir：launchd 环境下 PATH 里没有 node
    env["PATH"] = os.pathsep.join(paths + [env.get("PATH", "")])
    return env


def _cell_str(v: Any) -> str:
    """飞书单元格统一成字符串。单选/多选是数组，多选用「、」连起来。"""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or v.get("link") or "").strip()
    if isinstance(v, (list, tuple)):
        return "、".join([p for p in (_cell_str(x) for x in v) if p])
    return str(v).strip()


def _clean_link(s: str) -> str:
    """飞书里 url 类型的文本会被包成 markdown 链接：[文本](url)。"""
    m = _MD_LINK.match((s or "").strip())
    return (m.group(2) or m.group(1)).strip() if m else (s or "").strip()


def _clean_dt(s: str) -> str:
    """飞书给 ISO8601（带时区）或毫秒时间戳；看板要 'YYYY-MM-DD HH:MM'。"""
    s = (s or "").strip()
    m = _ISO_DT.match(s)
    if m:
        return "%s-%s-%s %s:%s" % m.groups()
    if s.isdigit() and len(s) >= 12:
        try:
            return datetime.fromtimestamp(int(s) / 1000).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            return ""
    return s


def parse_feishu_payload(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """lark-cli 返回的是「矩阵」（fields 列名 + data 行数组），转成通知列表。"""
    if not isinstance(obj, dict) or not obj.get("ok", True):
        raise ValueError("飞书返回 ok=false：%s" % str(obj.get("error", ""))[:200])
    data = obj.get("data") or {}
    cols = data.get("fields") or []
    rows = data.get("data") or []
    if not cols:
        return []
    idx = {c: i for i, c in enumerate(cols)}

    def g(row, name):
        i = idx.get(name)
        return _cell_str(row[i]) if (i is not None and i < len(row)) else ""

    out = []
    for row in rows:
        company = g(row, "公司名称")
        if not company:
            continue
        out.append({
            "公司名称": company,
            "岗位名称": g(row, "岗位名称"),
            "通知类型": [t for t in g(row, "通知类型").replace("，", "、").split("、") if t],
            "通知截止时间": _clean_dt(g(row, "通知截止时间")),
            "邮件链接": _clean_link(g(row, "邮件链接")),
            "邮件主题": g(row, "邮件主题"),
            "邮件收到时间": _clean_dt(g(row, "邮件收到时间")),
            "处理状态": g(row, "处理状态"),
        })
    return out


def fetch_notices_from_feishu(cfg: Config) -> List[Dict[str, Any]]:
    """调 lark-cli 从飞书多维表格拉通知。失败抛 RuntimeError，消息直接给用户看。"""
    cli = _find_lark_cli()
    if not cli:
        raise RuntimeError("没找到 lark-cli（飞书命令行）。装法见 README，或把 [notices] source 改回 file")
    env = _lark_env(cli)   # 顺手摘掉代理变量，见 _lark_env 的注释

    rows: List[Dict[str, Any]] = []
    offset = 0
    for _ in range(5):  # 一页 200 条，最多拉 1000 条
        args = [cli, "base", "+record-list",
                "--base-token", cfg.feishu_base, "--table-id", cfg.feishu_table,
                "--page-size", "200", "--offset", str(offset),
                "--format", "json", "--as", "user"]
        if cfg.feishu_profile:
            args[1:1] = ["--profile", cfg.feishu_profile]
        try:
            pr = subprocess.run(args, capture_output=True, timeout=90, env=env, cwd=cfg.base)
        except subprocess.TimeoutExpired:
            raise RuntimeError("调 lark-cli 超时（90 秒）")
        except OSError as e:
            raise RuntimeError("调 lark-cli 失败：%s" % e)
        if pr.returncode != 0:
            msg = (pr.stderr or pr.stdout or b"").decode("utf-8", "replace").strip()
            raise RuntimeError("lark-cli 退出码 %s：%s" % (pr.returncode, msg[:300]))
        try:
            obj = json.loads(pr.stdout.decode("utf-8", "replace"))
        except ValueError:
            raise RuntimeError("lark-cli 返回的不是 JSON")
        rows.extend(parse_feishu_payload(obj))
        if not (obj.get("data") or {}).get("has_more"):
            break
        offset += 200
    return rows


def write_notices_csv(cfg: Config, rows: List[Dict[str, Any]]) -> str:
    """通知写成看板要读的 CSV。先写临时文件再改名，避免留下半截文件。"""
    path = cfg.notices_csv
    if not path:
        raise RuntimeError("notices.csv 没配置")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    cols = NOTICE_FIELDS + ["发件人"]
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            d = dict(r)
            d["通知类型"] = "、".join(r.get("通知类型") or [])
            w.writerow(d)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# 岗位机会库（另一张飞书表：别人整理好的校招/实习信息汇总，八九千条）
# ---------------------------------------------------------------------------

OPP_PAGE = 500                 # ndjson 一页能稳拿 500 条（json 模式 200 就容易被掐断）
OPP_ROLE_MAX = 150             # 岗位列表原样能有几百字，截一段够认出来就行
OPP_MAX_PAGES = 60
OPP_CITY_COL = "地点②-筛地点选这里！！！！！！"   # 公式列，逗号拼好的城市；拿不到就退回多选列
OPP_CITY_ALT = "工作地点（筛选不要选这列）"

OPP_EXAM = {"含免笔试", "需要笔试", "仅测评"}     # 源表这列被塞过公司名，只认这三个值
OPP_JUNK = ("必读", "筛选前", "所属", "查看更", "文档", "说明")   # 表头提示误入选项的脏值
OPP_NOTE_COL = "备注/提示"

# 源表里混进来的"说明行"：公司名其实是「筛选前必看-点这里」这种卡片，不是岗位
OPP_JUNK_COMPANY = ("必看", "点这里", "筛选前")
_MD_LINK_ANY = re.compile(r"\[([^\]]*)\]\([^()]*\)")

# ---------------------------------------------------------------------------
# 「同一家公司不同分行」归并成一行
#
# 她的原话：「展现的时候把同一家公司不同分行放在同一个公司下面」。
# 源表把一家公司拆成很多条 —— 中国邮政储蓄银行 36 个「XX省/市分行」、招商银行 56 个、
# 中信期货 41 个分公司，一条一行的话列表里全是同一家公司的名字。
#
# 名字长得都是「主体 + 地名(可省) + 分支机构后缀」，所以规矩就两条：
#   ① 只有名字**以分支机构后缀结尾**时才动手，其余一律原样（宁可两家分行没并上，
#      也不能把两家不相干的公司并成一家）；
#   ② 切在**最后一个主体词**后面 —— 主体词 = 组织形式词 + 行业词（银行 / 期货 / 证券 …）。
#      多字词优先于单字词：某期货公司台州营业部的 base 是「某期货公司台州」，
#      单字「台」会切出「某期货公司台」（台州是地名），多字「期货」才不会切歪。
#   ③ 词表切不出来的（国航股份广东 / 中国邮政广西，各只有一条）走兜底：
#      在这批切不出来的名字里找最长公共前缀，「中信期货XX分公司」这么并回「中信期货」。
#      前缀至少 4 个字才认 —— 华能华北 / 华能玉环 的公共前缀只有「华能」，
#      与其赌它们是一家，不如就摆两行。
# ---------------------------------------------------------------------------
# 「总行 / 总部 / 支公司」也算分支机构名 —— 某银行总行、某集团总部、某保险公司佛山中心支公司
# 都是同一家的不同写法，不带上它们就会在主行旁边多冒一行「某银行总行」。
OPP_BRANCH_SUF = ("省分行", "市分行", "分公司", "分行", "支行", "营业部", "支公司",
                  "办事处", "分厂", "分店", "总行", "总部")

# 主体结尾词：名字里的「公司本体」通常以这些字收尾，切在它后面就是公司名。
# 前半是组织形式，后半是行业词。
OPP_BODY_END = (
    "股份有限公司", "有限责任公司", "有限公司", "集团有限公司", "集团", "公司",
    "总局", "总院", "研究院", "研究所", "设计院", "事业部", "中心", "大学", "学院",
    "医院", "学校", "银行", "信用社", "农商行", "期货", "证券", "基金", "信托",
    "人寿", "产险", "财险", "保险", "租赁", "保理", "投资", "控股", "实业",
    "电信", "移动", "联通", "通信", "电力", "能源", "石油", "石化", "天然气",
    "医药", "生物", "药业", "汽车", "教育", "航空", "航天", "物流", "航运",
    "科技", "网络", "电子", "电气", "机械", "化工", "钢铁", "建材", "地产",
    "置业", "建筑", "工程", "传媒", "文化", "旅游", "食品", "农业", "牧业",
    "矿业", "煤炭", "环保", "纺织", "服装", "家居", "电器", "仪表", "材料",
    # 单字词放在最后、且只在没有多字词可切时才用（见 opp_cut_body）
    "部", "局", "署", "社", "报", "行", "厂", "店", "所", "院",
)
OPP_STEM_MIN = 4        # 兜底时公共前缀至少这么长（太短 = 大概是两家公司）


def opp_clean_name(s: str) -> str:
    """归并用的一份名字：去掉括号注释和空白，免得「中信期货（上海）」和「中信期货上海」
    分成两家。括号里的内容在展示时照旧保留（只用来算归并键）。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", s or "")
    return re.sub(r"[\s\u3000]+", "", s).strip()


def opp_branch_base(n: str) -> Optional[str]:
    """名字以分支机构后缀结尾 → 返回去掉后缀的那截；否则 None（不动它）。"""
    for suf in OPP_BRANCH_SUF:
        if n.endswith(suf) and len(n) > len(suf):
            return n[: -len(suf)]
    return None


def opp_cut_body(b: str) -> Optional[str]:
    """在最后一个「主体词」后面切开。多字词优先，一个都没有才轮到单字词。

    「某银行云南省」→「某银行」（切在「银行」后）
    「某股份有限公司上海销售」→「某股份有限公司」
    「某建工局总承包」→「某建工局」（没有多字词，才用单字「局」）
    """
    best = -1
    for want_long in (True, False):
        for w in OPP_BODY_END:
            if (len(w) > 1) != want_long:
                continue
            i = b.rfind(w)
            if i >= 0:
                best = max(best, i + len(w))
        if best > 0:
            break
    return b[:best] if best > 0 else None


def opp_assign_keys(rows: List[Dict[str, Any]]) -> None:
    """给每条记录补一个归并键 `gk`：同一家公司的各条分行/岗位，gk 相同。

    键不一定是「正确的公司名」（比如「某银行福建省」这种也能当键），
    只要**同一家公司下面各条一致、不同公司之间不一样**就够了 —— 页面上显示的就是它。
    在 load_opportunities 里一次算完（跟 upd 一样是内存里的派生字段，不落盘）。
    """
    names = {opp_clean_name(r.get("company") or "") for r in rows}
    base_of: Dict[str, Optional[str]] = {}
    cut_of: Dict[str, Optional[str]] = {}
    for n in names:
        b = opp_branch_base(n)
        base_of[n] = b
        cut_of[n] = opp_cut_body(b) if b else None

    # 兜底池 = 带分支后缀、但词表切不出主体的那些
    pool: set = {base_of[n] for n in names if base_of[n] and not cut_of[n]}
    # 前缀计数：一次把所有 ≥ OPP_STEM_MIN 的前缀数出来，别在循环里反复扫池子
    pc: Dict[str, int] = {}
    for b in sorted(pool):
        for size in range(OPP_STEM_MIN, len(b) + 1):
            pc[b[:size]] = pc.get(b[:size], 0) + 1

    key_of: Dict[str, str] = {}
    for n in names:
        b = base_of[n]
        if not b:
            key_of[n] = n                       # 不带分支后缀：自己就是一家，原样
            continue
        cut = cut_of[n]
        if cut and len(cut) >= 2:
            key_of[n] = cut
            continue
        key_of[n] = b
        for size in range(len(b), OPP_STEM_MIN - 1, -1):
            if pc.get(b[:size], 0) >= 2:
                key_of[n] = b[:size]
                break

    for r in rows:
        r["gk"] = key_of.get(opp_clean_name(r.get("company") or "")) or (r.get("company") or "")


def _clean_md(s: str) -> str:
    """把正文里的 markdown 链接 [文字](url) 还原成文字。

    源表的公司名/备注里会带这玩意（如「小马智行[Pony.ai](http://Pony.ai)」），
    _clean_link 只处理「整格就是一个链接」的情况，这里处理夹在文字中间的。
    """
    s = _MD_LINK_ANY.sub(lambda m: (m.group(1) or "").strip(), s or "")
    return re.sub(r"\s{2,}", " ", s).strip()

# 源表「企业性质」写法不统一（民企 / 民营企业 / 私企 是同一个意思）。
# 表里显示的tag保留源表原话，但筛选项按下面这组归并 —— 否则同一个意思会分成好几条。
OPP_NATURE_MAP = (
    (("央国企", "央企", "国企"), "央国企"),
    (("银行",), "银行"),
    (("外企", "合资", "港澳台"), "外企/合资"),
    (("民企", "民营", "私企"), "民企"),
    (("事业单位",), "事业单位"),
    (("社会机构", "公益", "社会团体"), "社会组织"),
)

OPP_STATE: Dict[str, Any] = {"at": 0.0, "ok": None, "msg": "还没同步过",
                             "count": 0, "busy": False, "synced_at": "", "skipped": 0}
OPP_LOCK = threading.Lock()
OPP_RETRY_AFTER = 600          # 同步失败后隔多久允许重试（秒）

_RE_YMD = re.compile(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")
_RE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[-/月.]\s*(\d{1,2})\s*日?")


def opp_deadline_ts(s: str) -> Optional[float]:
    """截止时间是手写文本（"招满为止"、"2026-10-15"、"10月15日"），尽量抠出日期。

    抠不出来返回 None —— 调用方把它当"长期有效"，排序时沉到最后。

    ⚠️ 返回值是**当天末刻**（23:59:59），不是当天 00:00。
    「截止 10/15」= 10/15 一整天都还能投；按 00:00 算的话，当天一过零点，
    「还没截止」的判据（dl < now）立刻把它当过期藏起来 —— 2026-10-11 实测
    截止日=当天的 31 条全看不见。所以统一 +86399。
    """
    s = (s or "").strip()
    if not s:
        return None
    best = None
    for m in _RE_YMD.finditer(s):
        try:
            t = datetime(*(int(x) for x in m.groups())).timestamp()
        except ValueError:
            continue
        best = t if best is None else max(best, t)
    if best is not None:
        return best + 86399          # 当天末刻：截止日当天仍算「还没截止」
    m = _RE_MD.search(s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        now = datetime.now()
        for y in (now.year, now.year + 1):
            try:
                t = datetime(y, mo, d).timestamp()
            except ValueError:
                return None
            if t >= time.time() - 180 * 86400:
                return t + 86399     # 同上：末刻，别让当天截止的岗位一过零点就隐身
    return None


def _opp_list(v: Any, split_space: bool = False) -> List[str]:
    """多选/公式列统一成字符串列表。

    split_space=True 只给地点列用：公式列把多个城市拼成一条，有的行用逗号
    （"北京,苏州,上海"）、有的行用空格（"广州 深圳"），不按空格切就会把
    "广州 深圳" 当成一个城市名。
    """
    pat = r"[、,，;；\s]+" if split_space else r"[、,，;；]\s*"
    if v is None:
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                x = x.get("text") or x.get("name") or ""
            s = str(x).strip()
            if not s:
                continue
            if split_space:
                out.extend([t for t in re.split(pat, s) if t])
            else:
                out.append(s)
        return out
    s = str(v).strip()
    return [t for t in re.split(pat, s) if t] if s else []


def _opp_nature(v: Any) -> List[str]:
    """企业性质 → 归并后的大类（见 OPP_NATURE_MAP），用来做筛选项。"""
    out: List[str] = []
    for raw in _opp_list(v):
        name = "其他"
        for keys, label in OPP_NATURE_MAP:
            if any(k in raw for k in keys):
                name = label
                break
        if name not in out:
            out.append(name)
    return out


_RE_JIE = re.compile(r"(?<!\d)(\d{2,4})\s*届")


def _opp_targets(v: Any) -> List[str]:
    """招聘对象这列原文很杂："2027届"、"26届（留）-27届"、"所属：校招一表通"。

    统一只留 "20XX届"，一条记录可能横跨好几届就都留下；抠不出届数就丢掉。
    """
    out: List[str] = []
    for raw in _opp_list(v):
        for m in _RE_JIE.finditer(raw):
            n = int(m.group(1))
            if n < 1000:
                n += 2000
            if 2000 <= n <= 2100:
                s = "%d届" % n
                if s not in out:
                    out.append(s)
    if len(out) > 1:
        out.sort()
    return out


def parse_opp_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """源表一条记录 → 看板压缩后的结构（字段名用短的，8900 条省下不少体积）。"""
    f = rec.get("fields") or rec
    company = _clean_md(_cell_str(f.get("公司名称")))
    if not company or any(j in company for j in OPP_JUNK_COMPANY):
        return None
    roles = _clean_md(_cell_str(f.get("招聘岗位")))
    cities = _opp_list(f.get(OPP_CITY_COL), True) or _opp_list(f.get(OPP_CITY_ALT), True)
    out = {
        "id": rec.get("record_id") or rec.get("id") or "",
        "company": company,
        "industries": _opp_list(f.get("行业大类")),
        "nature": _opp_list(f.get("企业性质")),
        "nat": _opp_nature(f.get("企业性质")),
        "cities": cities,
        "roles": roles[:OPP_ROLE_MAX],
        "roles_more": max(0, len(roles) - OPP_ROLE_MAX),
        "deadline": _cell_str(f.get("截止时间")),
        "apply": _clean_link(_cell_str(f.get("投递方式"))),
        "notice": _clean_link(_cell_str(f.get("官方公告"))),
        "targets": _opp_targets(f.get("招聘对象")),
        "batch": [b for b in _opp_list(f.get("批次")) if not any(j in b for j in OPP_JUNK)],
        "exam": [e for e in _opp_list(f.get("文本 7")) if e in OPP_EXAM],
        "note": _clean_md(_cell_str(f.get(OPP_NOTE_COL))),
        "updated": _cell_str(f.get("更新时间")),
    }
    out["dl"] = opp_deadline_ts(out["deadline"])
    return out


OPP_SCRATCH = ".scratch"          # ndjson 落地的临时目录（在 data/ 下，已 gitignore）
OPP_ND_NAME = "opps.ndjson"       # 固定文件名 + --overwrite，每页直接覆盖，不用删任何文件


def fetch_opportunities_from_feishu(cfg: Config) -> List[Dict[str, Any]]:
    """分页拉整张汇总表。表很大、网络会抖，所以每页都给足重试。

    中途断了不白干：进度落在 data/.opps_progress.json，再跑一次接着拉。
    """
    cli = _find_lark_cli()
    if not cli:
        raise RuntimeError("没找到 lark-cli（飞书命令行）。装法见 README")
    env = _lark_env(cli)   # 顺手摘掉代理变量，见 _lark_env 的注释
    workdir = cfg.out_dir
    os.makedirs(workdir, exist_ok=True)
    scratch = os.path.join(workdir, OPP_SCRATCH)
    os.makedirs(scratch, exist_ok=True)
    out_rel = OPP_SCRATCH + "/" + OPP_ND_NAME      # 相对 cwd（= 上面那个 workdir）
    prog_path = os.path.join(workdir, ".opps_progress.json")

    prog: Dict[str, Any] = {"offset": 0, "rows": []}
    if os.path.exists(prog_path):
        try:
            with open(prog_path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and old.get("rows"):
                prog = {"offset": int(old.get("offset") or 0), "rows": old["rows"]}
        except (OSError, ValueError, TypeError):
            pass

    rows: List[Dict[str, Any]] = list(prog["rows"])
    skipped = 0
    offset, page_no = prog["offset"], 0
    while page_no < OPP_MAX_PAGES:
        obj, last = None, ""
        for _ in range(30):
            args = [cli, "base", "+record-list",
                    "--base-token", cfg.opp_base, "--table-id", cfg.opp_table,
                    "--limit", str(OPP_PAGE), "--offset", str(offset),
                    "--format", "ndjson", "--output", out_rel, "--overwrite", "--as", "user"]
            if cfg.opp_profile:
                args[1:1] = ["--profile", cfg.opp_profile]
            try:
                pr = subprocess.run(args, capture_output=True, timeout=240, env=env, cwd=workdir)
            except subprocess.TimeoutExpired:
                last = "超时"
                time.sleep(1.5)
                continue
            txt = (pr.stdout or b"").decode("utf-8", "replace").strip()
            got = None
            if txt.startswith("{"):
                try:
                    got = json.loads(txt)
                except ValueError:
                    got = None
            if got and (got.get("ok") is True or got.get("record_file")):
                obj = got
                break
            last = (json.dumps((got or {}).get("error") or {}, ensure_ascii=False)[:140]
                    if got else (txt[:140] or (pr.stderr or b"").decode("utf-8", "replace")[:140]))
            time.sleep(1.5)
        if not obj or not obj.get("record_file"):
            raise RuntimeError("第 %d 页没拉下来（%s）——已拉到 %d 条，再跑一次会接着拉"
                               % (page_no + 1, last or "未知错误", len(rows)))

        rf = obj["record_file"]
        if not os.path.isabs(rf):
            rf = os.path.join(workdir, rf)
        n0 = len(rows)
        with open(rf, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    r = parse_opp_record(rec)
                    if r:
                        rows.append(r)
                    else:
                        skipped += 1
        if len(rows) == n0 and (obj.get("records_count") or 0) > 0:
            # 有记录却一条都没解析出来，说明源表列名被改过，早点报错别写坏缓存
            raise RuntimeError("第 %d 页 0 条解析成功（源表列名可能改过了）" % (page_no + 1))

        page_no += 1
        OPP_STATE["msg"] = "正在同步…已拉 %d 条" % len(rows)
        if not obj.get("has_more"):
            break
        offset = obj.get("next_offset")
        if offset is None:
            offset += OPP_PAGE
        tmp = prog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"offset": offset, "rows": rows}, f, ensure_ascii=False)
        os.replace(tmp, prog_path)
        time.sleep(0.6)
    _drop(prog_path)
    OPP_STATE["skipped"] = skipped
    return rows


def _drop(path: str) -> None:
    """删临时文件。删不掉也无所谓，绝不能因为清理失败把整次同步带崩。"""
    try:
        os.remove(path)
    except Exception:
        pass


def write_opportunities(cfg: Config, rows: List[Dict[str, Any]]) -> str:
    path = cfg.opps_json
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"synced_at": now_str(), "count": len(rows), "rows": rows},
                  f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


_OPPS_CACHE: Dict[str, Any] = {"path": "", "mtime": 0.0, "rows": [], "synced_at": ""}
_FACET_CACHE: Dict[str, Any] = {"key": None, "val": None}


def day_start(ts: float = None) -> float:
    """某个时间戳所在那天的 0 点（本地时区）。不传就是今天。"""
    t = time.localtime(time.time() if ts is None else ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def parse_opp_date(s: Any) -> Optional[float]:
    """机会库的「更新时间」是文本，而且两种写法混着（2026/09/30 和 2026.04.27）。

    认得出就返回那天 0 点的时间戳；认不出（空、「尽快投递」这类）返回 None，
    调用方负责把 None 沉到最后 —— 别让解析失败把整表筛空。
    """
    m = re.match(r"^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*$", str(s or ""))
    if not m:
        return None
    try:
        y, mo, d = (int(x) for x in m.groups())
        return time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return None


# ---- 2026-10-08 AC-09：首次启动把随包的机会库快照预置进 data/ ----
# SPEC 一直写着「snapshot/opportunities.json 首次启动拷进 data/」，但全仓没有任何一处真的拷 ——
# 打包只带已跟踪文件，data/ 天然带不出去，所以同学解压后机会库那页是空的，
# 包里那 9381 条公开岗位等于白带。这里补上。

def _opps_snapshot_path() -> str:
    """随包快照的位置。**必须按脚本所在目录解析，不能用 cwd**：
    双击 start.command 与 LaunchAgent（开机自启）的工作目录不一样，
    用 cwd 的话只有「恰好在仓库根目录里跑」才找得到快照 —— 恰好是最不该依赖的巧合。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot", "opportunities.json")


def seed_opportunities_snapshot(cfg: "Config") -> int:
    """data/ 里还没有机会库缓存、而包里带了公开快照 → 拷一份过去。返回拷进的条数（0 = 没拷）。

    三条铁律，少一条都会变成「悄悄改用户的数据」：
      1. **绝不覆盖**已存在的 data/opportunities.json —— 用户自己同步下来的那份才是真相；
         拿到锁之后**再看一眼**（两个线程可能同时进来，谁先拷成谁赢）。
      2. 快照读不到 / 解不开 / 不是预期的结构 → **静默跳过**：不报错、不建空文件、不建空目录。
         快照是加分项，不是必需品 —— 它坏了不该让看板起不来。
      3. 落盘走原子写（临时文件 + 改名），断在半路也不会在 data/ 里留半个看起来能用的缓存
         （半个坏缓存比没有更糟：它会把真正的同步堵在「文件已存在」这一关外面）。
    """
    dst = cfg.opps_json
    src = _opps_snapshot_path()
    if not dst or os.path.exists(dst) or not os.path.isfile(src):
        return 0
    with APP_FILE_LOCK:          # 页面第一条请求 / 启动钩子 / 自动补全可能同时进来，拷一次就够
        if os.path.exists(dst):  # 拿到锁再看一眼，别的线程可能已经拷完了
            return 0
        try:
            with open(src, "rb") as f:
                raw = f.read()
            obj = json.loads(raw.decode("utf-8"))
            rows = obj.get("rows") if isinstance(obj, dict) else None
            if not isinstance(rows, list) or not rows:
                return 0          # 空快照拷过去没有意义，还平白占 6 兆
            tmp = dst + ".tmp"
            # data/ 目录在全新机器上还不存在（应用是懒创建的），先补上 ——
            # 不补的话 open 会直接 FileNotFoundError，快照就永远预置不进去
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(raw)      # 按字节原样拷，不重新序列化 —— 内容与随包文件逐字节一致
            os.replace(tmp, dst)
        except (OSError, ValueError):
            try:
                if os.path.exists(dst + ".tmp"):
                    os.remove(dst + ".tmp")
            except OSError:
                pass
            return 0
    print("机会库首次预置完成：%d 条公开岗位（来自随包快照）" % len(rows))
    return len(rows)


def load_opportunities(cfg: Config) -> Tuple[List[Dict[str, Any]], str]:
    """读机会库缓存。文件有好几兆，按 mtime 缓存住，别每个筛选请求都重读一遍。"""
    p = cfg.opps_json
    if not p or not os.path.exists(p):
        # 还没有缓存就先试着预置一份（serve 启动、页面首屏、--report、自动补全都从这条路径过 ——
        # 钩在**唯一**的装载入口上，才不会出现「起服务预置了、离线导出却没预置」的分裂）
        if seed_opportunities_snapshot(cfg) <= 0:
            return [], ""
        # 拷成了就接着往下读：第一屏就有数据，不用等下一次请求或下一次同步
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return [], ""
    c = _OPPS_CACHE
    if c["path"] == p and c["mtime"] == mtime and c["rows"]:
        return c["rows"], c["synced_at"]
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return (c["rows"], c["synced_at"]) if c["path"] == p else ([], "")
    rows = d.get("rows") or []
    # 补一个能直接比大小的时间戳：源表的「更新时间」是文本、两种写法混着，
    # 按字符串排会把 2026.04.27 排到 2026/09/30 后面去。
    for _r in rows:
        if "upd" not in _r:
            _r["upd"] = parse_opp_date(_r.get("updated"))
    # `dl` 是**持久化在 opportunities.json 里的**（上一次同步时算的）。解析口径（末刻）
    # 是唯一事实源，缓存里那份可能是老口径算出来的 —— 装载时一律重算，不然得等到下次
    # 飞书同步（TTL 6 小时）才生效，她今天就看不到当天截止的那几十条。
    # ⚠️ 只在内存里改，不回写 opportunities.json（那是她的数据）。
    for _r in rows:
        _r["dl"] = opp_deadline_ts(_r.get("deadline"))
    opp_assign_keys(rows)      # 同一家公司（各分行）的归并键，见 opp_assign_keys
    c.update({"path": p, "mtime": mtime, "rows": rows,
              "synced_at": str(d.get("synced_at") or "")})
    return rows, c["synced_at"]


def opp_facets_cached(rows: List[Dict[str, Any]], key: Any) -> Dict[str, List[List[Any]]]:
    if _FACET_CACHE["key"] == key and _FACET_CACHE["val"] is not None:
        return _FACET_CACHE["val"]
    val = opp_facets(rows)
    _FACET_CACHE.update({"key": key, "val": val})
    return val


def opps_ok_recently(cfg: Config) -> bool:
    at = OPP_STATE.get("at") or 0.0
    return bool(at) and (time.time() - at) < cfg.opp_ttl


def opps_seed_cache(cfg: Config) -> None:
    """进程刚起时 OPP_STATE 是空的，不补一下就会每次重启都白拉九千条。

    本地缓存还在、且没超过 TTL，就按缓存文件的写入时间当成"刚同步过"。
    """
    if OPP_STATE.get("at"):
        return
    rows, synced = load_opportunities(cfg)
    if not rows:
        return
    try:
        mt = os.path.getmtime(cfg.opps_json)
    except OSError:
        return
    OPP_STATE.update({"at": mt, "ok": True, "count": len(rows), "synced_at": synced,
                      "msg": "已同步 %d 条（读本地缓存）" % len(rows)})


def sync_opportunities(cfg: Config, force: bool = False, quiet: bool = False) -> Tuple[bool, str]:
    if cfg.opps_source != "feishu":
        return True, "数据源不是飞书"
    if not force and opps_ok_recently(cfg):
        return True, str(OPP_STATE["msg"])
    if not OPP_LOCK.acquire(blocking=False):
        return True, "正在同步"
    OPP_STATE["busy"] = True
    try:
        rows = fetch_opportunities_from_feishu(cfg)
        write_opportunities(cfg, rows)
        OPP_STATE.update({"at": time.time(), "ok": True, "count": len(rows),
                          "synced_at": now_str(), "msg": "已同步 %d 条" % len(rows)})
    except Exception as e:
        # 失败别按满 TTL 记 —— 否则网络抖一下、lark-cli 断一次，
        # 接下来 6 小时都判定"刚同步过"、再也不重试。改成 10 分钟后允许重试。
        retry = min(OPP_RETRY_AFTER, max(60, cfg.opp_ttl // 2))
        OPP_STATE.update({"at": time.time() - (cfg.opp_ttl - retry), "ok": False, "msg": "%s" % e})
        if not quiet:
            print("✗ 机会库同步失败：%s" % e)
        return False, "%s" % e
    finally:
        OPP_STATE["busy"] = False
        OPP_LOCK.release()
    if not quiet:
        print("✓ 机会库已同步 %d 条 → %s%s" % (
            OPP_STATE["count"], cfg.opps_json,
            ("（跳过 %d 条说明行/没公司名的）" % OPP_STATE["skipped"])
            if OPP_STATE.get("skipped") else ""))
    return True, str(OPP_STATE["msg"])


def sync_opportunities_bg(cfg: Config, force: bool = False) -> None:
    """后台拉一次，这一请求不等它。已经在拉了就直接返回（拿锁失败的那个线程会自己退）。"""
    if OPP_STATE.get("busy"):
        return
    threading.Thread(target=sync_opportunities,
                     kwargs={"force": force, "quiet": True},
                     args=(cfg,), daemon=True).start()


def sync_watchdog(cfg: Config, tick: int = 300) -> None:
    """看门狗线程：只要服务在跑，机会库/投递进度一过期就自己拉，不必等页面被打开。

    以前判 TTL 只写在 GET /api/opps 里 —— 服务开着、但页面停在别的标签上，
    机会库就永远不刷新（用户以为"电脑开着就会同步"，其实没有）。
    这个线程把那个缺口补上：服务活着 = 数据会自己更新。
    """
    time.sleep(min(tick, 30))   # 起服务后缓一下，别和启动流程抢 lark-cli
    while True:
        try:
            if cfg.opps_source == "feishu":
                opps_seed_cache(cfg)      # 进程刚起时 OPP_STATE 为空，先认本地缓存的 mtime
                if not OPP_STATE.get("busy") and not opps_ok_recently(cfg):
                    sync_opportunities_bg(cfg)
            # 投递进度一样：飞书表里改了状态，看板不该等你点刷新才跟上。
            # 机会库正在拉的时候先让一让，两个 lark-cli 别同时抢。
            if cfg.apps_source == "feishu":
                apps_seed_state(cfg)
                if (not APP_STATE.get("busy") and not apps_ok_recently(cfg)
                        and not OPP_STATE.get("busy")):
                    sync_apps_bg(cfg)
            # 邮箱通知（IMAP）也一样：电脑开着就该自己回扫一遍，不必等你点开这一页。
            if cfg.notices_source == "imap":
                notices_seed_state(cfg)
                if not SYNC_LOCK.locked() and not sync_ok_recently(cfg):
                    sync_notices_bg(cfg)
        except Exception:
            pass                          # 看门狗自己绝不能死，下一轮再来
        time.sleep(tick)


def opp_facets(rows: List[Dict[str, Any]]) -> Dict[str, List[List[Any]]]:
    """每个可选字段的全部取值 + 条数。一条不截 —— 前端「展开全部」要用。"""
    def top(key: str, n: int = 0):
        c: Dict[str, int] = {}
        for r in rows:
            for v in (r.get(key) or []):
                c[v] = c.get(v, 0) + 1
        items = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
        return [[k, v] for k, v in (items[:n] if n else items)]
    return {
        "industries": top("industries"),
        "cities": top("cities"),
        "batch": top("batch"),
        "targets": top("targets"),
        "exam": top("exam"),
        "nature": top("nat"),
    }


def opp_stats(rows: List[Dict[str, Any]], apps: List[Dict[str, Any]]) -> Dict[str, int]:
    """整张表的几个总数，给 KPI 卡用。跟当前筛选无关。"""
    mine = set()
    for a in apps:
        n = norm_company(a.get("公司名称") or "")
        if n:
            mine.add(n)
    now = time.time()
    d0 = day_start()
    open_n = y27 = mine_n = exam_n = new1 = new3 = new7 = 0
    for r in rows:
        t = r.get("dl")
        if t is None or t >= now:
            open_n += 1
        if "2027届" in (r.get("targets") or []):
            y27 += 1
        if "含免笔试" in (r.get("exam") or []):
            exam_n += 1
        u = r.get("upd")
        if u is not None:
            age = round((d0 - u) / 86400.0)
            if age == 0:
                new1 += 1
            if 0 <= age <= 2:
                new3 += 1
            if 0 <= age <= 6:
                new7 += 1
        if mine:
            n = norm_company(r.get("company") or "")
            if n and any(n == m or (len(n) >= 2 and len(m) >= 2 and (n in m or m in n)) for m in mine):
                mine_n += 1
    return {"all": len(rows), "open": open_n, "y2027": y27, "mine": mine_n, "exam": exam_n,
            "new1": new1, "new3": new3, "new7": new7}


def opp_haystack(r: Dict[str, Any]) -> str:
    """搜索命中范围：整条记录里所有能读懂的文字（表里 14 个字段都在里面）。"""
    parts = [r.get("company") or "", r.get("roles") or "", r.get("note") or "",
             r.get("deadline") or ""]
    for k in ("industries", "nat", "nature", "cities", "targets", "batch", "exam"):
        parts.append(" ".join(r.get(k) or []))
    return " ".join(parts).lower()


# 搜索框可以「先选字段、再填词」：键是传给接口的 qf/qxf 值，值是下拉里显示的名字。
# 空键 = 全部字段（老行为，也是默认）。
OPP_SEARCH_FIELDS = [
    ("", "全部字段"),
    ("co", "公司名称"),
    ("role", "招聘岗位"),
    ("ind", "行业"),
    ("city", "城市"),
    ("nat", "企业性质"),
    ("target", "招聘对象"),
    ("batch", "批次"),
    ("note", "备注"),
    ("exam", "免笔试"),
    ("deadline", "截止时间"),
    ("updated", "更新时间"),
]
OPP_SEARCH_FIELD_KEYS = {k for k, _ in OPP_SEARCH_FIELDS}


def opp_field_text(r: Dict[str, Any], field: str) -> str:
    """搜索用的一条记录文字：field 为空（或认不出来）就给整条，否则只给那一个字段。"""
    if not field:
        return opp_haystack(r)
    if field == "co":
        return (r.get("company") or "").lower()
    if field == "role":
        return (r.get("roles") or "").lower()
    if field == "note":
        return (r.get("note") or "").lower()
    if field == "deadline":
        return (r.get("deadline") or "").lower()
    if field == "updated":
        return (r.get("updated") or "").lower()
    # 下面几个在表里是列表：拼成一串再搜，"上海 北京" 里找 "上海" 照样命中
    if field == "ind":
        vals = r.get("industries") or []
    elif field == "city":
        vals = r.get("cities") or []
    elif field == "nat":
        vals = r.get("nature") or r.get("nat") or []
    elif field == "target":
        vals = r.get("targets") or []
    elif field == "batch":
        vals = r.get("batch") or []
    elif field == "exam":
        vals = r.get("exam") or []
    else:
        return opp_haystack(r)      # 字段名不认识（老链接 / 手改 URL）→ 退回整条
    return " ".join(vals).lower()


def query_opportunities(rows: List[Dict[str, Any]], apps: List[Dict[str, Any]],
                        params: Dict[str, List[str]]) -> Dict[str, Any]:
    """按条件筛 + 排序 + 分页。params 是 parse_qs 的结果（值都是列表）。

    搜索是「包含」和「不包含」两块、**同时生效**，每块里都能挂**多条**「字段 + 词」条件：
    - `q` / `qf` 是**可重复的平行数组**：第 i 个词串配第 i 个字段。
      页面上每条条件发一对，所以多条条件各查各的字段，互不干扰。
      `qf[i]` 缺失（手写 URL 时）就等于「全部字段」；字段名认不出来也一样。
      页面永远成对发送，所以这条兜底只影响手写链接。
    - 包含：**每条条件内**的词要全部命中，**条件之间**是「且」；
    - 不包含：只要**任一条条件**命中就整条排掉（跟包含同时生效）；
    - 同一字段在一条记录上只算一次文字（`cache`），条件多了也不会翻几遍。
    - `qnot=1` 是 v0.7.0 及以前的旧口径（把 q 整块当排除用），只为兼容老链接保留。
    """
    def one(k, d=""):
        v = params.get(k) or []
        return (v[0] or d).strip() if v else d

    def conds(word_key: str, field_key: str) -> List[Tuple[str, List[str]]]:
        """把 q/qf 这种平行数组配成 [(字段, [词…]), …]。空词的条件直接不要。"""
        words = params.get(word_key) or []
        fields = params.get(field_key) or []
        out = []
        for i, raw in enumerate(words):
            # 字段是「修饰」，没配到就退回全部字段 —— 手写 URL 少给一个 qf 也不会串味
            f = ((fields[i] if i < len(fields) else "") or "").strip()
            if f not in OPP_SEARCH_FIELD_KEYS:
                f = ""          # 认不出的字段同样退回整条，别让人为的手写参数把结果筛空
            terms = [t for t in re.split(r"\s+", (raw or "").lower()) if t]
            if terms:
                out.append((f, terms))
        return out

    inc = conds("q", "qf")
    exc = conds("qx", "qxf")
    exclude = one("qnot") == "1"
    ind, city, batch = one("ind"), one("city"), one("batch")
    nat = one("nat")
    co = one("co")
    co_n = norm_company(co) if co else ""
    target, exam, sort = one("target"), one("exam"), one("sort", "dl")
    rev = one("rev") == "1"
    only_open = one("open") == "1"
    # 「最近新开」：只看最近 N 天内更新过的。源表只有「更新时间」、没有「首次收录」，
    # 所以口径是「最近有动静」—— 新开的岗位必然在里面，被重新编辑的老岗位也会进来。
    try:
        fresh = max(0, min(365, int(one("newdays", "0"))))
    except ValueError:
        fresh = 0
    fresh_cut = (day_start() - (fresh - 1) * 86400.0) if fresh else 0.0
    only_mine = one("mine") == "1"
    try:
        limit = min(200, max(10, int(one("limit", "60"))))
        offset = max(0, int(one("offset", "0")))
    except ValueError:
        limit, offset = 60, 0

    mine = set()
    if only_mine:
        for a in apps:
            n = norm_company(a.get("公司名称") or "")
            if n:
                mine.add(n)

    now = time.time()
    out = []
    for r in rows:
        if ind and ind not in (r.get("industries") or []):
            continue
        if city and city not in (r.get("cities") or []):
            continue
        if nat and nat not in (r.get("nat") or []):
            continue
        if batch and batch not in (r.get("batch") or []):
            continue
        if target and target not in (r.get("targets") or []):
            continue
        if exam and exam not in (r.get("exam") or []):
            continue
        if co_n:
            n = norm_company(r.get("company") or "")
            if not n or not (n == co_n or co_n in n or n in co_n):
                continue
        if fresh:
            u = r.get("upd")
            if u is None or u < fresh_cut:
                continue
        if only_open:
            t = r.get("dl")
            if t is not None and t < now:
                continue
        if mine:
            n = norm_company(r.get("company") or "")
            if not any(n and (n == m or (len(n) >= 2 and len(m) >= 2 and (n in m or m in n))) for m in mine):
                continue
        if inc or exc or exclude:
            cache: Dict[str, str] = {}      # 同一条记录里，同一个字段只拼一次文字

            def ftext(f: str) -> str:
                if f not in cache:
                    cache[f] = opp_field_text(r, f)
                return cache[f]

            if exclude:                # 旧口径（qnot=1）：q 里的词在整条记录里出现任何一个就排掉，不分字段
                if any(t in ftext("") for _, ts in inc for t in ts):
                    continue
            else:
                # 包含：每条条件里的词都得在它自己选的字段里找得到
                if any(not all(t in ftext(f) for t in ts) for f, ts in inc):
                    continue
                # 不包含：任一条条件命中就整条排掉（跟包含同时生效）
                if any(any(t in ftext(f) for t in ts) for f, ts in exc):
                    continue
        out.append(r)

    # —— 按公司归并：一家一行，点开才摊开各分行/岗位 ——
    # ⚠️ 归并只能在这里做、不能挪到前端：分页是按**公司**分的，前端拿到 60 条记录再归并，
    # 就会把「同一家公司在第 1 页和第 2 页各一半」的症状做出来。
    gmap: Dict[str, List[Dict[str, Any]]] = {}
    gorder: List[str] = []
    for r in out:
        k = r.get("gk") or (r.get("company") or "")
        if k not in gmap:
            gmap[k] = []
            gorder.append(k)
        gmap[k].append(r)

    def merged(rs: List[Dict[str, Any]], field: str) -> List[str]:
        seen, vals = set(), []
        for r in rs:
            for v in (r.get(field) or []):
                if v not in seen:
                    seen.add(v)
                    vals.append(v)
        return vals

    groups = []
    for k in gorder:
        rs = gmap[k]
        # 截止时间取「组里最近的一个还没过期的」—— 一家公司有几条、各自截止不同，
        # 列表上该显示的是「最该去投的那个」；全过期了才退回最早的那个。
        fut = [r["dl"] for r in rs if r.get("dl") is not None and r["dl"] >= now]
        pas = [r["dl"] for r in rs if r.get("dl") is not None]
        groups.append({
            "key": k, "company": k, "n": len(rs),
            # 组里带分支后缀（分行/分公司/营业部）的条数 —— 页面据此决定写「N 个分行」还是「N 个岗位」
            "bn": sum(1 for r in rs if opp_branch_base(opp_clean_name(r.get("company") or ""))),
            "industries": merged(rs, "industries"), "cities": merged(rs, "cities"),
            "targets": merged(rs, "targets"), "nat": merged(rs, "nat"),
            "batch": merged(rs, "batch"), "exam": merged(rs, "exam"),
            "dl": min(fut) if fut else (min(pas) if pas else None),
            "upd": max([r["upd"] for r in rs if r.get("upd") is not None], default=None),
            "applies": sum(1 for r in rs if r.get("apply")),
            "notices": sum(1 for r in rs if r.get("notice")),
            "rows": rs,
        })

    if sort == "co":
        groups.sort(key=lambda g: g["key"], reverse=rev)
    elif sort == "ind":
        groups.sort(key=lambda g: (g["industries"] or ["\uffff"])[0], reverse=rev)
    elif sort == "up":
        # 按真实先后排（源表两种日期写法混着，按字符串排会乱）；
        # 认不出更新时间的一律沉到最底 —— 正序反序都一样，它们没有「先后」可比
        groups.sort(key=lambda g: g["upd"] or 0.0, reverse=not rev)
        groups.sort(key=lambda g: g["upd"] is None)
    else:
        # 认不出截止日期的（招满为止 / 尽快投递）永远沉到最后，反向排也一样
        groups.sort(key=lambda g: g["upd"] or 0.0, reverse=True)
        groups.sort(key=lambda g: (g["dl"] is None,
                                   -float(g["dl"] or 0) if rev else float(g["dl"] or 0)))

    page = groups[offset:offset + limit]
    return {"total": len(groups), "total_rows": len(out), "offset": offset, "limit": limit,
            # rows 是所有被选中的组摊平后的记录（老调用方还在用），groups 是页面真正渲染的东西
            "groups": page, "rows": [r for g in page for r in g["rows"]]}


SYNC_LOCK = threading.Lock()
SYNC_STATE: Dict[str, Any] = {"at": 0.0, "ok": None, "msg": "还没同步过", "count": 0}


def notice_ttl(cfg: Config) -> int:
    """通知来源不同，节流时间也不同：飞书走 lark-cli（一次 2~5 秒），邮箱回扫要连服务器。"""
    return cfg.mail_ttl if cfg.notices_source == "imap" else cfg.feishu_ttl


def sync_ok_recently(cfg: Config) -> bool:
    at = SYNC_STATE.get("at") or 0.0
    return bool(at) and (time.time() - at) < notice_ttl(cfg)


def notices_seed_state(cfg: Config) -> None:
    """进程刚起时，先把本地 CSV 的改动时间认成「上次同步时间」——
    这样重启后不会一上来就白拉一次，页面也能立刻写出「上次几点同步的」。"""
    if SYNC_STATE.get("at"):
        return
    if cfg.notices_csv and os.path.exists(cfg.notices_csv):
        try:
            SYNC_STATE["at"] = os.path.getmtime(cfg.notices_csv)
        except OSError:
            pass


def sync_mail_notices(cfg: Config, force: bool = False, quiet: bool = False) -> Tuple[bool, str]:
    """连邮箱抓一轮（ADR-001）。TTL 内不重复拉。**唯一的联网路径，且只在 imap 数据源下才会走。**"""
    if not cfg.mail_enabled:
        return False, "邮箱没配（[mail] 的 user / authcode）"
    if not force and sync_ok_recently(cfg):
        return True, str(SYNC_STATE["msg"])
    if not SYNC_LOCK.acquire(blocking=False):
        return True, "正在同步"
    try:
        raws = mail_fetch(cfg)
        stats = mail_build_notices(cfg, raws)
        msg = ("扫了 %d 封：新增 %d 条通知（%d 条没认出类型，已在列表里标「待确认」），"
               "没认出公司 %d，跳过 %d（含非秋招的杂邮，已按关键词滤掉）"
               % (len(raws), stats["added"], stats["unknown"], stats["unmatched"], stats["skipped"]))
        if stats.get("applied"):
            msg += "；拒信 / Offer 归档到投递进度 %d 条" % stats["applied"]
        SYNC_STATE.update({"at": time.time(), "ok": True, "count": stats["notices"], "msg": msg})
    except Exception as e:      # 登录/网络/解析都可能出问题，一律降级成看板上的一行提示
        SYNC_STATE.update({"at": time.time(), "ok": False, "msg": "%s" % e})
        if not quiet:
            print("✗ 邮箱同步失败：%s" % e)
        return False, "%s" % e
    finally:
        SYNC_LOCK.release()
    if not quiet:
        print("✓ %s → %s" % (SYNC_STATE["msg"], cfg.notices_csv))
    return True, str(SYNC_STATE["msg"])


def sync_notices(cfg: Config, force: bool = False, quiet: bool = False) -> Tuple[bool, str]:
    """按数据源把最新通知同步成本地 CSV。TTL 内不重复拉。"""
    if cfg.notices_source == "imap":
        return sync_mail_notices(cfg, force=force, quiet=quiet)
    if cfg.notices_source != "feishu":
        return True, "数据源不是飞书 / 邮箱"
    if not force and sync_ok_recently(cfg):
        return True, str(SYNC_STATE["msg"])
    if not SYNC_LOCK.acquire(blocking=False):
        return True, "正在同步"
    try:
        rows = fetch_notices_from_feishu(cfg)
        write_notices_csv(cfg, rows)
        SYNC_STATE.update({"at": time.time(), "ok": True, "count": len(rows),
                           "msg": "已同步 %d 条" % len(rows)})
    except Exception as e:  # 网络/授权/格式都可能出问题，一律降级成看板上的一行提示
        SYNC_STATE.update({"at": time.time(), "ok": False, "msg": "%s" % e})
        if not quiet:
            print("✗ 飞书通知同步失败：%s" % e)
        return False, "%s" % e
    finally:
        SYNC_LOCK.release()
    if not quiet:
        print("✓ 飞书通知已同步 %d 条 → %s" % (SYNC_STATE["count"], cfg.notices_csv))
    return True, str(SYNC_STATE["msg"])


def sync_notices_bg(cfg: Config) -> None:
    """后台线程里同步，别卡住 /api/board 那次请求。"""
    threading.Thread(target=sync_notices, args=(cfg,), kwargs={"quiet": True},
                     daemon=True).start()


def ensure_notices(cfg: Config) -> None:
    """一次性命令（--list / --report）用：先同步好再读，
    否则 board_payload 起的后台线程会随进程退出夭折，拿到的还是旧缓存。"""
    if cfg.notices_source in ("feishu", "imap"):
        sync_notices(cfg, quiet=True)


# ---------------------------------------------------------------------------
# 手动补的通知（短信 / 电话来的，邮箱收不到）—— 只存本机 data/notices_manual.json
# ---------------------------------------------------------------------------
# 为什么单独一个文件、而不是并进 notices.csv / notice_states.json：
#   · notices.csv 是**缓存** —— sync_notices() 每 5 分钟就用 write_notices_csv() 从飞书
#     **整份重写**一遍，写进去的手动行会被冲掉；
#   · notice_states.json 是「处理状态」的旁路覆盖（{notice_key: 状态} 的 dict），
#     一条通知一个 key，装不下整行。
# 手动行只存在本机、**不回写飞书**（反向同步这次不做）。字段用跟 notices.csv 一模一样的中文列名，
# 下游（排序 / 筛选 / 归纳 / kpi / attach_notices / appOf）就一个都不用改。
MANUAL_NOTICES_LOCK = threading.Lock()   # 只管 notices_manual.json，别复用同步专用的 SYNC_LOCK


def notices_manual_path(cfg: Config) -> str:
    return os.path.join(cfg.out_dir, "notices_manual.json")


def load_manual_notices(cfg: Config) -> List[Dict[str, Any]]:
    """读本机手动补的通知。文件不存在 / 内容坏了都当没有 —— 绝不让看板因此打不开
    （照 load_notice_states 的容错风格）。"""
    path = notices_manual_path(cfg)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(obj, list):
        return []
    out: List[Dict[str, Any]] = []
    for r in obj:
        if not isinstance(r, dict) or not str(r.get("公司名称") or "").strip():
            continue
        d = dict(r)
        ts = d.get("通知类型")
        if isinstance(ts, str):
            d["通知类型"] = [t for t in ts.replace("，", "、").split("、") if t]
        elif not isinstance(ts, list):
            d["通知类型"] = []
        out.append(d)
    return out


def save_manual_notices(cfg: Config, rows: List[Dict[str, Any]]) -> None:
    """先写 .tmp 再 os.replace（跟 write_notices_csv 一样的原子写法），避免留下半截文件。"""
    path = notices_manual_path(cfg)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with MANUAL_NOTICES_LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


# 截止时间只收「用空格分隔」的那一种（前端负责把 datetime-local 的 T 换成空格）。
_MANUAL_DT = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$")


def add_manual_notice(cfg: Config, **f: Any) -> Tuple[bool, str, str]:
    """新增一条手动通知，落 notices_manual.json。返回 (成功?, 错误信息, id)。

    去重只跟**手动表内部**已有的行比（按 mail_row_key）：完全一样就复用旧 id、不新增。
    故意**不跟飞书来的行**去重 —— 她手写的东西不能因为「邮件那条也到了」就消失。
    """
    company = str(f.get("公司名称", "") or "").strip()
    if not company:
        return (False, "公司名不能空", "")
    company = company[:40]                      # 太长就截断，不报错
    ntype = str(f.get("类型", "") or "").strip()
    if ntype and ntype not in KNOWN_MAIL_TYPES:
        return (False, "类型只能是：" + " / ".join(KNOWN_MAIL_TYPES), "")
    dl = str(f.get("截止时间", "") or "").strip()
    if dl and not _MANUAL_DT.match(dl):
        return (False, "时间格式不对（要 2026-10-16 12:00 这样）", "")
    link = str(f.get("链接", "") or "").strip()
    if link and "://" not in link:
        link = "https://" + link
    link = link[:300]
    note = str(f.get("说明", "") or "").strip()[:200]
    pos = str(f.get("岗位名称", "") or "").strip()[:60]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row: Dict[str, Any] = {
        "公司名称": company,
        "岗位名称": pos,
        "通知类型": [ntype] if ntype else [],
        "通知截止时间": dl,
        "邮件链接": link,
        # 说明存进「邮件主题」那列（通知表那列的表头就叫「主题」）
        "邮件主题": note,
        "邮件收到时间": now,
        "处理状态": "",
        "创建时间": now,
    }
    rows = load_manual_notices(cfg)
    new_key = mail_row_key(row)
    for r in rows:
        if mail_row_key(r) == new_key:
            return (True, "", str(r.get("id", "")))   # 完全重复：复用旧 id，不新增
    mid = uuid.uuid4().hex[:8]
    row["id"] = mid
    rows.append(row)
    save_manual_notices(cfg, rows)
    return (True, "", mid)


def del_manual_notice(cfg: Config, mid: str) -> bool:
    """按 id 删一条手动通知。删完顺手清掉挂在它 key 上的处理状态，别留孤儿。"""
    mid = str(mid or "").strip()
    if not mid:
        return False
    rows = load_manual_notices(cfg)
    hit = None
    rest: List[Dict[str, Any]] = []
    for r in rows:
        if str(r.get("id", "")) == mid:
            hit = r
        else:
            rest.append(r)
    if hit is None:
        return False
    save_manual_notices(cfg, rest)
    try:
        key = notice_state_key(hit)
        if key and key in load_notice_states(cfg):
            set_notice_state(cfg, key, "")      # 写法与 set_notice_state 一致（读改写、原子落盘）
    except (OSError, ValueError, TypeError):
        pass
    return True


def load_notices(cfg: Config) -> List[Dict[str, Any]]:
    """读邮箱通知 CSV（qqmail2table 的产物）。按公司+类型+截止+主题去重。"""
    path = cfg.notices_csv
    if not path or not os.path.exists(path):
        return []
    seen: Dict[tuple, Dict[str, Any]] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ts = [t for t in (r.get("通知类型") or "").replace("，", "、").split("、") if t]
                key = (r.get("公司名称", ""), "、".join(ts),
                       r.get("通知截止时间", ""), r.get("邮件主题", ""))
                if key not in seen:
                    co = r.get("公司名称", "")
                    seen[key] = {
                        "公司名称": co,
                        "岗位名称": r.get("岗位名称", ""),
                        "通知类型": ts,
                        "通知截止时间": r.get("通知截止时间", ""),
                        "邮件链接": r.get("邮件链接", ""),
                        "邮件主题": r.get("邮件主题", ""),
                        "邮件收到时间": r.get("邮件收到时间", ""),
                        "处理状态": r.get("处理状态", ""),
                        "原始处理状态": r.get("处理状态", ""),
                        "状态来源": "飞书" if r.get("处理状态") else "",
                        "_key": notice_state_key({
                            "公司名称": co, "通知类型": ts,
                            "通知截止时间": r.get("通知截止时间", ""),
                            "邮件主题": r.get("邮件主题", ""),
                        }),
                        # 「同公司归纳」的组键（2026-10-08 她拍板：统计到面板时同公司归到一起）。
                        # **精确匹配**，只去掉首尾空白 —— 绝不许按名字猜母子合并，
                        # app_parents.json 那套归并只属于投递进度表，通知归纳就是同名字归同组。
                        # 内存派生、不落盘（跟 upd 的做法一致）；空公司名归「（未知公司）」组，
                        # 不许用空串当键 —— 空串键会把所有没名字的混成一坨，页面上还显示不出来。
                        "_gkey": (co or "").strip() or "（未知公司）",
                    }
    except OSError:
        return []
    out = list(seen.values())
    # 手动补的通知无条件并进来 —— **不参与上面那个 seen 去重**（故意的：她手写的东西
    # 不能因为「邮件那条也到了」就消失）。只跟手动表内部去重（add_manual_notice 里做）。
    # 每行补全 _key / _gkey，前四个派生字段跟 CSV 行**完全一致**，
    # 这样排序 / 筛选 / 归纳 / kpi 卡 / attach_notices / appOf 一个都不用改。
    for m in load_manual_notices(cfg):
        co = str(m.get("公司名称", "") or "").strip()
        ts = m.get("通知类型") or []
        if not isinstance(ts, list):
            ts = [t for t in str(ts).replace("，", "、").split("、") if t]
        st = str(m.get("处理状态", "") or "")
        out.append({
            "公司名称": co,
            "岗位名称": m.get("岗位名称", ""),
            "通知类型": ts,
            "通知截止时间": m.get("通知截止时间", ""),
            "邮件链接": m.get("邮件链接", ""),
            "邮件主题": m.get("邮件主题", ""),
            "邮件收到时间": m.get("邮件收到时间", ""),
            "处理状态": st,
            "原始处理状态": st,
            "状态来源": "本地" if st else "",
            "_key": notice_state_key({
                "公司名称": co, "通知类型": ts,
                "通知截止时间": m.get("通知截止时间", ""),
                "邮件主题": m.get("邮件主题", ""),
            }),
            "_gkey": co or "（未知公司）",
            "来源": "手动",
            "_mid": str(m.get("id", "")),
        })
    return out


def apply_notice_states(cfg: Config, notices: List[Dict[str, Any]]) -> int:
    """把本地手动标的状态盖上去（就地改 notices），返回覆盖了几条。
    飞书里你标过的照旧，本地改过的以本地为准——这样同步不会把你的改动冲掉。"""
    local = load_notice_states(cfg)
    if not local:
        return 0
    hit = 0
    for n in notices:
        st = local.get(n.get("_key", ""))
        if st:
            n["处理状态"] = st
            n["状态来源"] = "本地"
            hit += 1
    return hit


def attach_notices(apps: List[Dict[str, Any]], notices: List[Dict[str, Any]]) -> None:
    """给每条投递挂上「这家公司发来过哪些通知」，就地改 apps。

    比对用 same_company_tight 而不是共用件 same_company：后者允许 2 字裸子串，
    会把「京东」的通知挂到「京东方」那行上（两家完全不一样，2026-10-09 她亲口纠正）。
    """
    for a in apps:
        hits = [n for n in notices if same_company_tight(a.get("公司名称", ""), n.get("公司名称", ""))]
        types: List[str] = []
        for n in hits:
            for t in n.get("通知类型", []):
                if t not in types:
                    types.append(t)
        next_dl = ""
        for n in sorted(hits, key=lambda x: x.get("通知截止时间") or "9999"):
            if n.get("通知截止时间"):
                next_dl = n["通知截止时间"]
                break
        a["关联"] = {"count": len(hits), "types": types, "next": next_dl}


# ---------------------------------------------------------------------------
# 邮箱直连（IMAP）—— ADR-001 / ADR-002
# ---------------------------------------------------------------------------
# 这一段是从隔壁 qqmail-job-tracker/qqmail2table.py 移植进来的（同一套规则、同样的
# 「宁可漏过不可错认」）。相对原版只改三处（ADR-002 §1）：
#   1) 五类改为：资料补充 / AI面 / 笔试 / 测评 / 面试（顺序即优先级）；
#      「笔试」从原来的「测评」里拆出来 —— 看板的面试阶梯里 测评 与 笔试 本就是两个节点；
#   2) 「AI面」不再含「视频面试」（视频面试可能是真人面，归到「面试」）；
#   3) 否定类（感谢信 / 宣讲会 / 退订…）不冒充五类里的任何一类，单独落 data/mail_skipped.jsonl 备查。
#
# 全程零第三方依赖、纯规则：每条判定都说得清「命中哪个词、在主题还是正文、发件人是谁」，
# 不调大模型、不外发邮件内容，可解释、可复现、可审计（ADR-002）。

# 五类通知。顺序 = 优先级（越具体越靠前）。
KNOWN_MAIL_TYPES = ["资料补充", "AI面", "笔试", "测评", "面试"]

MAIL_TYPE_RULES: List[Tuple[str, List[str]]] = [
    ("资料补充", ["补充材料", "补充资料", "提交材料", "上传材料", "完善简历", "资料审核",
                  "资格审核", "资格复审", "补充信息", "证件上传", "信息确认", "材料提交"]),
    ("AI面", ["ai面", "ai 面", "ai面试", "ai 面试", "智能面试", "机器人面试", "数字面试",
              "ai video interview", "one-way interview", "vi面"]),
    ("笔试", ["在线笔试", "笔试邀请", "笔试通知", "笔试安排", "参加笔试", "笔试试卷",
              "written test", "笔试"]),
    # ⚠️ 裸「测评」是 2026-10-08 用户明确要求放宽的（原来只认「在线测评 / 测评邀请」这类组合词）：
    # 有的公司邮件就写「测评通知」三个字，不放宽整封就漏了。
    # 代价用户也已经认了 —— 已经结束的测评（「测评结果反馈」那种）也会被标成测评，
    # 所以这里**不许**再加否定词去筛：滤掉就是违背裁定。优先级顺序也别动。
    ("测评", ["在线测评", "测评邀请", "能力测评", "综合测评", "人才测评", "性格测试",
              "认知能力", "assessment", "online test", "测评"]),
    ("面试", ["面试邀请", "面试通知", "面试安排", "面试", "初面", "复面", "一面", "二面",
              "终面", "群面", "无领导", "interview", "面邀"]),
]

# 否定类。命中即「不是我们能用的通知」，落 data/mail_skipped.jsonl，绝不冒充五类里的任何一类。
# 清单口径对齐作者的邮箱→飞书自动化提示词（规则源，2026-10-08 移植进直连引擎）：
# 投递回执 / 满意度调查 / 验证码 / 平台推广。
# ⚠️ 拒信词**不在这里**（见 MAIL_REJECT_HINTS）—— 2026-10-08 她追加：拒信不许静默丢，
# 要么进投递进度、要么进通知表标「拒信」，所以拒信走的是 verdict="reject" 这条路，不是 skip。
MAIL_NEGATIVE_HINTS = [
    # 投递成功回执（"已收到你的投递 / 报名成功 / 投递成功"）
    "已收到你的投递", "已收到您的投递", "报名成功", "投递成功",
    # 面试满意度调查（"满意度 / 问卷 / 评价您的面试"）
    "满意度", "问卷", "评价您的面试", "评价你的面试",
    # 邮箱验证码
    "验证码", "校验码",
    # 招聘平台推广（"智联推荐 / 校招日历 / 职位推荐"）
    "智联推荐", "校招日历", "职位推荐",
    "宣讲会", "空中宣讲", "宣讲邀请", "退订", "招聘推送",
]

# 拒信词：**能单独站住**的硬词 + MAIL_REJECT_PAIRS 的成对匹配。
# 命中 → verdict="reject"，认出公司后分两路（见 mail_apply_verdict）：
# 匹配到投递记录 → 打止步（止步来源=邮件）；匹配不到 → 进通知表标「拒信」。都不丢。
MAIL_REJECT_HINTS = ["感谢信", "未通过", "遗憾地通知", "已挂", "不再推进", "不再考虑"]

# Offer / 录用词。同样不丢：匹配到投递记录 → 状态推「Offer」（手改过的绝不覆盖）；
# 匹配不到 → 进通知表标「Offer」。必须**先过拒信关**——「很遗憾，这次未通过」的
# 邮件主题可能就写着「录用结果通知」。
#
# ⚠️⚠️ 2026-10-09 大修（她报的红框：4 行假 Offer）。原来只有一条词表
# `MAIL_OFFER_HINTS = ["录用","录取","意向书","offer"]`，判定方式是
# `词.lower() in (主题 + 正文前3000字).lower()`。这条在真实邮箱里必炸，实测栽过：
#   · 「【XX校园招聘】在线人才测评邀请」正文里出现一次「录用」→ 测评邀请判成 Offer
#   · 「简历已收到，谢谢你选择 XX 有限公司」正文里有个英文 offer → 投递回执判成 Offer
#   · 「XX科技2027校园招聘简历接收函」正文里有「三方协议 / 录用通知」模板段 → 接收函判成 Offer
# 根因是**正文根本不能信**：页脚、退订链接、模板段落、营销文案里都有这两个字。
# 所以现在分三档，且**只看主题**（主题是发信方自己写的标题，噪音比正文小一个量级）：
#   · 强词：本身就是"录用文书"的名字 → 主题命中即认定，状态可直接推
#   · 弱词：单独出现可能只是"流程里提了一句" → 主题命中算 Offer，但**推状态要有旁证**
#     （这家得先有过测评/笔试/AI面/面试类通知 —— 她的原话：真录用了，前面几封面试通知不可能没有）
#   · 排除词：主题明写着是简历回执 / 还没筛完 → 一票否决，绝不当录用
# 正文里出现的一律**不算**。宁可漏：漏了的邮件会完整进通知表，她看得见、能自己点。
MAIL_OFFER_STRONG = ["录用通知", "录用通知书", "录用函", "聘用通知", "意向书", "入职通知",
                     "入职", "报到", "三方协议", "签约", "offer letter"]
MAIL_OFFER_WEAK = ["录用", "录取", "offer"]
MAIL_OFFER_NOT = ["简历接收函", "简历已收到", "收到简历", "简历投递成功", "投递成功",
                  "感谢投递", "简历筛选", "人才库", "感谢信", "感谢与祝福"]
# 旧名字（自检与外部脚本按它找词表）：并集
MAIL_OFFER_HINTS = MAIL_OFFER_STRONG + MAIL_OFFER_WEAK


def _mail_word_in(word: str, low: str) -> bool:
    """词在（已转小写的）文本里出现没有。**纯英文/数字词按整词匹配**。

    加词边界是必须的：`"offer" in 正文` 会打中 offers / offering / 页脚文案，
    `"fast" in 正文` 会打中 breakfast，`"sea" in 正文` 会打中 research ——
    真实邮箱里这三样全都让她的看板出过错（2026-10-09）。中文没有词边界，照常子串。
    """
    w = (word or "").lower()
    if not w:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9 ]*", w):
        return re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low) is not None
    return w in low


def mail_offer_hit(subject: str) -> Tuple[List[str], bool]:
    """主题里有没有录用信号。返回 (命中词, 是不是强词)。

    **只看主题**：正文里的「录用 / offer」一律不算（理由见上面词表注释）。
    排除词优先 —— 主题写着「简历接收函 / 简历已收到 / 感谢投递」的一律不算录用。
    """
    low = (subject or "").lower()
    if not low:
        return [], False
    if any(_mail_word_in(w, low) for w in MAIL_OFFER_NOT):
        return [], False
    strong = [w for w in MAIL_OFFER_STRONG if _mail_word_in(w, low)]
    if strong:
        return strong, True
    return [w for w in MAIL_OFFER_WEAK if _mail_word_in(w, low)], False


# 「这封录用邮件有旁证」要认的通知类型（见 mail_offer_corroborated）。
MAIL_CORROB_TYPES = ("测评", "笔试", "AI面", "面试", "资料补充")

# 拒信**成对**匹配：「感谢」+ 坏消息词同现才拒 —— 拒信的标准开头是
# 「感谢您的关注，很遗憾…」，而 offer 邮件也是「感谢您参加…现正式录用」。
# 只有左词没有右词（比如纯 offer 的"感谢"）绝不拒。
MAIL_REJECT_PAIRS: List[Tuple[str, List[str]]] = [
    ("感谢", ["遗憾", "未能", "不匹配", "无法录用", "暂不录用", "不录用", "抱歉地通知"]),
]

# 催办 / 提醒类主题词。同公司同类型已有记录的邮件，主题再命中这些词就只是催，
# 不另立一行（见 mail_build_notices 的去重段）。
MAIL_BUMP_HINTS = ["提醒", "催办", "再次", "未完成", "尽快完成", "截止在即"]

# 「秋招相关性」闸（2026-10-08 她明确要的：只收秋招面试/测评这类邮件，别的不要）。
# 类型认不出、但公司认得出的邮件，主题/正文里必须命中这里的词才进看板（标「待确认」）；
# 一个都不命中 = 购物/物流/会员通知这类杂邮，直接跳过 —— 机会库公司名是子串匹配，
# 杂邮碰巧含某个公司名就能对上，没有这道闸会灌进来几百条垃圾（实测 460 条）。
MAIL_RECRUIT_HINTS = [
    "校招", "秋招", "春招", "校园招聘", "招聘", "面试", "测评", "笔试",
    "offer", "录用", "投递", "简历", "网申", "ai面", "一面", "二面", "三面",
    "终面", "初面", "复试", "人才计划", "管培生", "管培", "实习",
]

# 判定「截止时间」时要看上下文的词。日期附近出现这些词，权重更高。
MAIL_DEADLINE_HINTS = ["截止", "前完成", "之前完成", "有效期", "有效时间", "请于", "请在",
                       "务必在", "最后期限", "deadline", "expire", "due", "valid until",
                       "作答时间", "有效期至"]

# 日期前面出现这些词，说明它是「面试时间 / 地点」而不是截止时间，要降权。
MAIL_DEADLINE_NEGATIVE_HINTS = ["面试时间", "面试地点", "笔试时间", "考试时间", "开始时间",
                                 "活动时间", "宣讲时间", "到场时间", "时间（", "地点"]

# 日期 / 时间候选。按精度从高到低尝试。
MAIL_DATE_PATTERNS: List[Tuple[str, str]] = [
    (r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*[T ]?\s*(\d{1,2})\s*[:：]\s*(\d{2})", "ymdhm"),
    (r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?", "ymd"),
    (r"(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*[T ]?\s*(\d{1,2})\s*[:：]\s*(\d{2})", "mdhm"),
    (r"(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日", "md"),
]

# 「N 天内 / N 小时内 / N 个工作日内」这类相对截止。很多邮件只写相对时间，按邮件时间往后推。
MAIL_RELATIVE_DEADLINE_PATTERNS: List[Tuple[str, str]] = [
    (r"(?:接下来|未来|之后|从即日起)\s*的?\s*(\d{1,3}|[一二三四五六七八九十]+)\s*天", "day"),
    (r"(\d{1,3}|[一二三四五六七八九十]+)\s*天\s*(?:之)?内", "day"),
    (r"(\d{1,3}|[一二三四五六七八九十]+)\s*个?\s*工作日\s*(?:之)?内", "workday"),
    (r"(\d{1,3}|[一二三四五六七八九十]+)\s*个?\s*小时\s*(?:之)?内", "hour"),
    # 「收到本通知起的72小时」—— 不带「内」的口径（「起」字上下文跟时长）。
    # 跟上一条重叠时两条算出同一个值，min() 不受影响。
    (r"起\s*的?\s*(\d{1,3})\s*个?\s*小时", "hour"),
]

# 相对时间附近出现这些词，说明它是客服话术（多久回你），不是截止线。
MAIL_RELATIVE_NEGATIVE_HINTS = ["会与您联系", "会与你联系", "我们会与", "回复您", "回复你",
                                "通知您", "通知你", "尽快与您", "尽快与你"]

# 相对时间的天数上限，超过就不当截止线（「一年内有效」这种不是本次截止）。
MAIL_RELATIVE_MAX = {"day": 90, "workday": 60, "hour": 720}

# 链接里出现这些片段就当噪音丢掉（退订、追踪、社交）。
MAIL_URL_NOISE = ["unsubscribe", "退订", "list-manage", "mailchi.mp", "utm_source",
                  "weibo.com", "weixin.qq.com/r/", "privacy", "policy", "help"]

# 发件人展示名去掉这些后缀就是公司名候选。
MAIL_SENDER_NOISE_SUFFIX = ["校园招聘", "招聘团队", "招聘组", "人力资源部", "人力资源",
                            "招聘中心", "校招", "招聘", "hr", "no-reply", "noreply",
                            "recruit", "careers", "campus", "hr团队"]

# 比对用的「主体名」要额外剥掉的地域 / 机构尾巴（ADR-002 引 ARCH §4.1）。**只用于比对**，
# 显示名永远用用户名单里的原名 —— 「XX银行杭州分行」对得上「XX银行」，但看板上还是写原名。
MAIL_BRANCH_SUFFIX = ["省", "市", "自治区", "地区", "分行", "支行", "分公司", "办事处",
                      "营业部", "研究院", "研究所", "事业部", "总部"]

# 未匹配区（认不出公司的邮件）的落盘列。
# 类型拿不准但公司认得出的那些**不落这里** —— 它们是通知表里的正常一行，类型留空。
MAIL_UNMATCHED_FIELDS = ["发件人", "发件人域名", "邮件主题", "邮件收到时间", "摘要"]

# =?UTF-8?B?...?= / =?GB2312?Q?...?= 手工拆分用（decode_header 挂了时的兜底）
_MAIL_RFC2047_RE = re.compile(r"=\?([^?]+)\?([BbQq])\?([^?]*)\?=")


def _mail_decode_header(raw: Optional[str]) -> str:
    """解码 RFC2047 编码的邮件头。标准库 decode_header 遇到损坏的 base64 会直接抛异常，
    而这一抛会把整封通知邮件丢掉 —— 真实邮件里确实有编码不规范的标题。所以自己兜一层：
    拆得动就拆，拆不动退回原文，绝不整封丢。"""
    if not raw:
        return ""
    try:
        chunks = decode_header(raw)
    except Exception:
        chunks = []
        for m in _MAIL_RFC2047_RE.finditer(raw):
            charset, mode, text = m.group(1), m.group(2).upper(), m.group(3)
            try:
                if mode == "B":
                    data = base64.b64decode(text + "=" * (-len(text) % 4))
                else:
                    data = quopri.decodestring(text.replace("_", " ").encode("ascii"))
                chunks.append((data, charset))
            except Exception:
                continue
        leftover = _MAIL_RFC2047_RE.sub("", raw).strip()
        if leftover:
            chunks.append((leftover, None))
        if not chunks:
            chunks = [(raw, None)]

    parts: List[str] = []
    for chunk, enc in chunks:
        if isinstance(chunk, bytes):
            for candidate in (enc, "utf-8", "gb18030", "latin-1"):
                if not candidate:
                    continue
                try:
                    parts.append(chunk.decode(candidate, errors="strict"))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _mail_decode_payload(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    for cs in (part.get_content_charset(), "utf-8", "gb18030"):
        if not cs:
            continue
        try:
            return payload.decode(cs, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _mail_strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
                .replace("&quot;", '"'))
    return re.sub(r"[ \t\u3000]+", " ", html)


def mail_extract_body(msg) -> str:
    """拿到邮件正文纯文本。优先 text/plain，没有再用 text/html 去标签。"""
    plain: List[str] = []
    html: List[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if "attachment" in str(part.get("Content-Disposition") or "").lower():
            continue
        if ctype == "text/plain":
            plain.append(_mail_decode_payload(part))
        elif ctype == "text/html":
            html.append(_mail_strip_html(_mail_decode_payload(part)))
    text = "\n".join(plain) if plain else ("\n".join(html) if html else "")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def mail_extract_links(msg, text: str) -> List[str]:
    raw: List[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "multipart" and part.get_content_type() == "text/html":
            raw.extend(re.findall(r'href=["\'](https?://[^"\']+)["\']', _mail_decode_payload(part)))
    raw.extend(re.findall(r"https?://[^\s<>\"'）)】\]]+", text))
    out: List[str] = []
    seen = set()
    for url in raw:
        url = url.rstrip(".,;:!)】")
        low = url.lower()
        if any(n in low for n in MAIL_URL_NOISE) or url in seen:
            continue
        seen.add(url)
        out.append(url)
    # 越像「去作答」的链接排越前
    out.sort(key=lambda u: 0 if re.search(
        r"apply|assess|test|exam|interview|job|campus|recruit|talent|hire", u, re.I) else 1)
    return out


def _mail_rank(c: Dict[str, Any]) -> Tuple[int, int]:
    """一个候选类型的**完整排序键**：`(负分数, 优先级序号)` —— 越小越优。

    ⚠️ `mail_classify` 的排序和 `mail_decide` 的「谁赢了」**必须共用这一个键**。
    2026-10-08 抓的就是两边口径不一致：排序带了优先级那一维，并列判定却只比分数，
    于是加回裸「测评」之后，「笔试测评通知」两边都命中、同为 2 分 → 判成并列 → 类型留空。
    本来认得出的邮件反而变成认不出 —— 这不是用户认的代价，是真的退化。
    `KNOWN_MAIL_TYPES` 那句「顺序即优先级」本来就是拿来打破平局的，
    排序键写了它、判定没用它，属于漏了半个口径。
    """
    t = c["type"]
    return (-c["score"], KNOWN_MAIL_TYPES.index(t) if t in KNOWN_MAIL_TYPES else 99)


def mail_classify(subject: str, body: str, domain_ok: bool) -> List[Dict[str, Any]]:
    """按五类规则给每个候选类型打分（ADR-002 §2）。

    得分：主题命中类别词 +2（强）；发件人域名已在你自己的别名表里 +1（强）；
          正文命中 ≥2 个不同关键词 +1（弱）。
    只命中一个正文关键词得 0 分 —— 也算候选，但后面会被「≤1 进待确认」挡掉。这就是「宁可漏过」。
    """
    subj, bod = (subject or "").lower(), (body or "").lower()
    cands: List[Dict[str, Any]] = []
    for typ, kws in MAIL_TYPE_RULES:
        s_hits = [k for k in kws if k.lower() in subj]
        b_hits = [k for k in kws if k.lower() in bod]
        if not s_hits and not b_hits:
            continue
        score = 0
        if s_hits:
            score += 2
        if len(b_hits) >= 2:
            score += 1
        if domain_ok:
            score += 1
        cands.append({"type": typ, "score": score, "sub": s_hits, "body": b_hits})
    cands.sort(key=_mail_rank)
    return cands


def mail_decide(subject: str, body: str, domain_ok: bool) -> Tuple[str, str, Dict[str, Any]]:
    """定类型。返回 (类型, 结论, 依据)。结论文 notice / review。

    三道闸（都可解释）：
      · 最高分 ≤ 1（只有弱信号）→ review，不标类型；
      · 两个类型**排序键完全相同** → review，不猜（同分但优先级不同不算并列，按优先级取）；
      · 「AI面」压过「面试」（AI面邮件正文里几乎都会出现「面试」二字）。
    抽不到就留空、拿不准就降级 —— 看错一封，这个工具在用户那儿就废了。
    """
    cands = mail_classify(subject, body, domain_ok)
    if not cands:
        return "", "review", {"为什么": "没命中任何类型词"}

    top_score = cands[0]["score"]
    top_rank = _mail_rank(cands[0])
    winners = [c for c in cands if _mail_rank(c) == top_rank]
    if len(winners) > 1 and all(c["type"] in ("AI面", "面试") for c in winners):
        # ⚠️ 这条**已经被 _mail_rank 的优先级那一维覆盖**了（AI面 index 1 < 面试 index 4，
        # 同分必然由 AI面 胜出），留着只是双保险。别看它「像是在生效」就去动它 ——
        # 真正起作用的是优先级顺序，不是这一行。
        # ⚠️️ 但**绝不能**把它写成「只要 candidates 里有 AI面，就把「面试」整条删掉」：
        # 那样一封明确的面试通知（主题「终面安排通知」，正文只顺口提了一句「含 AI面试 环节」）
        # 会从「认得出」被打成「认不出」—— 2026-10-08 实际踩过一次，
        # 删「面试」只能删在**已经并列**的这一小撮里，不能删在全集里。
        winners = [c for c in winners if c["type"] == "AI面"]
    if top_score <= 1:
        return "", "review", {
            "为什么": "只有正文里的弱信号（不到两个关键词，或没有主题佐证），不敢定类型",
            "候选": [c["type"] for c in winners],
            "正文命中": sorted({k for c in winners for k in c["body"]}),
        }
    if len(winners) > 1:
        return "", "review", {
            "为什么": "两个类型的分数与优先级都完全一样，分不出主次",
            "并列": [c["type"] for c in winners],
        }
    w = winners[0]
    ev: Dict[str, Any] = {
        "规则": w["type"],
        "命中词": list(w["sub"]) + list(w["body"]),
        "位置": "主题" if w["sub"] else "正文",
        "域名已在别名表": bool(domain_ok),
    }
    # 同分靠优先级决出胜负的，必须写进依据 —— 静默决胜就变成猜了，跟「可解释」的底线冲突。
    tied = [c["type"] for c in cands
            if c["score"] == top_score and c["type"] != w["type"]]
    if tied:
        ev["同分"] = sorted([w["type"]] + tied)
        ev["优先级"] = "同分，按类型优先级 资料补充 > AI面 > 笔试 > 测评 > 面试 取靠前的那个"
    return w["type"], "notice", ev


_MAIL_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                   "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _mail_mk_date(y, m, d, hh=23, mm=59) -> Optional[datetime]:
    if int(hh) == 24:              # 「24:00」= 当天最后一刻
        hh, mm = 23, 59
    try:
        return datetime(int(y), int(m), int(d), int(hh), int(mm))
    except ValueError:
        return None


def _mail_to_int(s: str) -> Optional[int]:
    """把「3」或「三十」「十五」这类转成整数，转不了返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _MAIL_CN_DIGITS.get(a, 1) if a else 1
        ones = _MAIL_CN_DIGITS.get(b, 0) if b else 0
        return tens * 10 + ones
    total = 0
    for ch in s:
        if ch not in _MAIL_CN_DIGITS:
            return None
        total = total * 10 + _MAIL_CN_DIGITS[ch]
    return total


def mail_extract_relative_deadline(text: str, mail_dt: datetime) -> Optional[datetime]:
    """找「N 天内 / N 小时内 / N 个工作日内」这类相对截止，从邮件时间往后推。
    只在找不到绝对日期时才用。多个候选取最早的（宁可早提醒，不要漏）。"""
    cands: List[datetime] = []
    for pattern, unit in MAIL_RELATIVE_DEADLINE_PATTERNS:
        for m in re.finditer(pattern, text):
            n = _mail_to_int(m.group(1))
            if not n or n <= 0 or n > MAIL_RELATIVE_MAX[unit]:
                continue
            ctx = text[max(0, m.start() - 20): m.end() + 20]
            if any(h in ctx for h in MAIL_RELATIVE_NEGATIVE_HINTS):
                continue
            if unit == "hour":
                delta = timedelta(hours=n)
            elif unit == "workday":
                delta = timedelta(days=n + (n // 5) * 2)   # 5 个工作日 ≈ 7 个自然日
            else:
                delta = timedelta(days=n)
            cands.append(mail_dt + delta)
    return min(cands) if cands else None


def mail_extract_deadline(text: str, mail_dt: datetime, tz_off: int,
                          default_days: int = 0) -> Optional[datetime]:
    """找截止时间。四层：
      1) 扫所有日期候选，前后 40 字里有 DEADLINE_HINTS 就加分；
      2) 取分数最高、落在 [今天-1天, 今天+365天] 的候选；
      3) 没有绝对日期 → 试「N 天内 / N 小时内」这类相对表述；
      4) 还没有 → 按 default_days 兜底（默认 0 = **不兜底**，抽不到就留空，绝不按邮件时间硬推）。
    """
    now = datetime.now()
    lo, hi = now - timedelta(days=1), now + timedelta(days=365)
    scored: List[Tuple[int, datetime]] = []
    for pattern, kind in MAIL_DATE_PATTERNS:
        for m in re.finditer(pattern, text):
            g = m.groups()
            if kind == "ymdhm":
                dt = _mail_mk_date(g[0], g[1], g[2], g[3], g[4])
            elif kind == "ymd":
                dt = _mail_mk_date(g[0], g[1], g[2])
            elif kind == "mdhm":
                dt = _mail_mk_date(mail_dt.year, g[0], g[1], g[2], g[3])
                if dt and dt < now - timedelta(days=1):
                    dt = dt.replace(year=dt.year + 1)
            else:                                   # md
                dt = _mail_mk_date(mail_dt.year, g[0], g[1])
                if dt and dt < now - timedelta(days=1):
                    dt = dt.replace(year=dt.year + 1)
            if not dt or not (lo <= dt <= hi):
                continue
            ctx = text[max(0, m.start() - 40): m.end() + 40]
            score = 0
            if any(h.lower() in ctx.lower() for h in MAIL_DEADLINE_HINTS):
                score += 10
            if kind in ("ymdhm", "mdhm"):
                score += 3                          # 带具体时间点的更可能是真截止时间
            pre = text[max(0, m.start() - 12): m.start()]
            if any(h in pre for h in MAIL_DEADLINE_NEGATIVE_HINTS):
                score -= 8                          # 「面试时间：…」这种是安排表，不是截止线
            scored.append((score, dt))
    if scored:
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[0][1]
    rel = mail_extract_relative_deadline(text, mail_dt)
    if rel:
        return rel
    if default_days > 0:
        end = mail_dt + timedelta(days=default_days)
        return end.replace(hour=23, minute=59, second=0, microsecond=0)
    return None


def _mail_clean_position(pos: str) -> str:
    """把「某公司2027届市场营销岗」这类前缀去掉，只留岗位名。"""
    pos = pos.strip(" ·-—、")
    m = re.search(r"(?:20\d{2}\s*届?|第\s*\d+\s*届)", pos)
    if m and m.end() < len(pos):
        pos = pos[m.end():].lstrip(" ·-—、")
    return pos.strip()


def mail_extract_position(subject: str, text: str) -> str:
    """岗位名：正文里的「岗位：xxx」最可靠，其次从标题里找…方向 /…岗。**抽不到留空，不猜。**"""
    m = re.search(r"(?:应聘|申请|投递|报名|职位|岗位|方向)\s*(?:名称)?\s*[:：]\s*"
                  r"([^\n，,。；;：:]{2,40})", text)
    if m:
        return _mail_clean_position(m.group(1))
    m = re.search(r"(?:投递|应聘|申请|报名)(?:的|了)?\s*"
                  r"([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,30}?"
                  r"(?:管理培训生|培训生|管培生|实习生|工程师|专员|经理|顾问|方向|岗位|岗))", text)
    if m:
        return _mail_clean_position(m.group(1))
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9（）()·\-]{2,30}?(?:管理培训生|培训生|管培生|实习生|"
                  r"工程师|专员|经理|顾问|方向|岗位|岗))", subject)
    if m:
        return _mail_clean_position(m.group(1))
    return ""


def _mail_company_candidate(subject: str, body: str, from_name: str) -> str:
    """从标题里切一个公司名候选。**只拿去跟用户自己的名单比对，不作最终结论**；
    切不出来就退回发件人展示名（去掉「校园招聘」这类尾巴），再切不出就空串。
    **绝不拿域名硬凑一个公司名**（ARCH §4：只在用户名单里认，不从邮件臆造）。"""
    patterns = [
        r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,30}?(?:集团|公司|银行|证券|研究所|事务所|事业部|中国区|控股))",
        r"^([\u4e00-\u9fa5A-Za-z0-9·]{2,20}?)(?:20\d{2}|校园招聘|校招|招聘|邀请|通知)",
    ]
    for p in patterns:
        m = re.search(p, subject or "")
        if m:
            return m.group(1).strip(" ·-—、")
    name = from_name or ""
    for suffix in MAIL_SENDER_NOISE_SUFFIX:
        name = re.sub(re.escape(suffix) + r"$", "", name, flags=re.I).strip()
    return name if len(name) >= 2 else ""


def norm_company_core(s: str) -> str:
    """比对专用的「主体名」：在 norm_company 基础上再剥掉地域 / 机构尾巴。
    只用于比对，不用于显示 —— 显示名永远取用户名单里的原名（ARCH §4.1）。"""
    s = norm_company(s)
    changed = True
    while changed and len(s) > 2:
        changed = False
        for suf in MAIL_BRANCH_SUFFIX:
            if s.endswith(suf) and len(s) - len(suf) >= 2:
                s = s[:-len(suf)]
                changed = True
    return s


def mail_same_company(a: str, b: str) -> bool:
    """公司名比对：先用 same_company，再退回剥了地域/机构的「主体名」。
    让「XX银行杭州分行」对得上「XX银行」。

    ⚠️ 2026-10-09 收紧：**子串包含必须短名 ≥3 个字**。裸子串会把 2 字短名并进
    任何含它的名字里 —— 「京东」⊂「京东方」，于是 campus@jd.com 的校招信落到了
    「京东方」那一行上（她报的红框里正有这条）。归并不上就退回通知表，不静默丢。
    """
    if not a or not b:
        return False
    for fa, fb in ((norm_company(a), norm_company(b)),
                   (norm_company_core(a), norm_company_core(b))):
        if not fa or not fb:
            continue
        if fa == fb:
            return True
        lo, hi = sorted([fa, fb], key=len)
        if len(lo) >= 3 and lo in hi:
            return True
    return False


def _mail_domain(addr: str) -> str:
    a = (addr or "").strip().lower()
    return a.split("@")[-1] if "@" in a else ""


def mail_aliases_path(cfg: "Config") -> str:
    return os.path.join(cfg.out_dir, "mail_aliases.json")


def load_mail_aliases(cfg: "Config") -> Dict[str, str]:
    """发件人域名 / 完整地址 → 公司名（用户在看板上点过一次的归类）。键统一小写。"""
    path = mail_aliases_path(cfg)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in obj.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            out[k.strip().lower()] = v.strip()
    return out


def set_mail_alias(cfg: "Config", key: str, company: str) -> bool:
    """写一条「这个发件人域名 = 这家公司」。company 传空 = 删掉这条（回到未匹配）。
    下次同域名的邮件就自动对上了 —— 越用越准，且全程无大模型。"""
    key = str(key or "").strip().lower()
    company = str(company or "").strip()
    if not key:
        return False
    path = mail_aliases_path(cfg)
    obj: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                obj = loaded
        except (OSError, ValueError):
            obj = {}
    if company:
        obj[key] = company
    else:
        obj.pop(key, None)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return True


def mail_name_hit(subject: str, from_name: str, body: str,
                  names: List[str], from_addr: str = "") -> Dict[str, Any]:
    """邮件专用的「这封是哪家」—— 比 pick_company_hit 严一档（2026-10-09 修红框那 4 行）。

    三处收紧，全是真实翻过的车：
      1) **不在删了空白的文本上数数**。`pick_company_hit` 走 `_co_key()`（把所有空白删掉）
         再裸数子串，于是
           · 「GalCiv IV O…」去空格成 `civivov…` → 命中「vivo」（2024 年的游戏广告信）
           · 「research hub」→ 命中「Sea」
           · 「京东 方面」去空格成「京东方面」→ 命中「京东方」
         邮件里的中文名从来不会跨空格连写，删空白只有坏处。
      2) **纯英文/数字的名字必须在主题或发件人里整词出现**，正文里出现一律不算 ——
         英文正文里 `fast` 藏在 breakfast、`sea` 藏在 research，裸子串等于随机。
      3) 纯英文名的"很确定"门槛从 4 个字提到 5 个字 —— 但**发件人展示名 / 发件地址里
         整词命中的照旧算数**（`vivo` 的四字母真身就是靠 `vivo招聘` / `hr-campus.vivo.com`
         认出来的，不能因为短就彻底不认；酒店信里的 "sea view" 则会因为不在发件人里而出局）。
    中文名沿用原来口径（正文里出现也算、4 个字以上算确定），避免功能退化。
    """
    sender_l = ("%s\n%s" % (from_name or "", from_addr or "")).lower()
    head_l = ("%s\n%s" % (subject or "", sender_l)).lower()
    body_l = (body or "").lower()
    seen: Dict[str, int] = {}
    seen_sender: Dict[str, int] = {}
    for raw in names:
        name = str(raw or "").strip()
        k = _co_key(name)
        if len(k) < 2:
            continue
        if re.fullmatch(r"[a-z0-9]+", k):
            pat = r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])"
            c = len(re.findall(pat, head_l))
            cs = len(re.findall(pat, sender_l))
        else:
            c = head_l.count(k) + body_l.count(k)
            cs = 0
        if c:
            seen[name] = c
            if cs:
                seen_sender[name] = cs
    out: Dict[str, Any] = {"hit": "", "sure": False, "cands": [], "n": 0}
    if not seen:
        return out
    ranked = sorted(seen.items(), key=lambda kv: (-len(_co_key(kv[0])), -kv[1], kv[0]))
    out["cands"] = [n for n, _c in ranked[:8]]
    out["n"] = len(ranked)
    top = ranked[0][0]
    kt = _co_key(top)
    if re.fullmatch(r"[a-z0-9]+", kt):
        sure = len(kt) >= 5 or top in seen_sender
    else:
        sure = len(kt) >= 4 or len(ranked) == 1
    if sure:
        out["hit"] = top
    out["sure"] = sure
    return out


def mail_match_company(subject: str, body: str, from_name: str, from_addr: str,
                       ctx: Dict[str, Any]) -> Tuple[str, str, str]:
    """把一封邮件对到用户自己名单里的公司。三级（ARCH §4.1），命不中返回空串。

    1) 别名直命中（域名 → 公司名，用户归类过的）；
    2) 名字匹配：先在邮件文本里认用户名单里已有的公司名，再拿邮件里切出的公司候选
       跟名单比（含剥了地域/机构的「主体名」）；
    3) 域名匹配（弱，仅当 1/2 都没命中，且域名主体 ≥4 字符时）。
    **只在用户名单里匹配，绝不从正文臆造一个公司名出来。**
    """
    domain = _mail_domain(from_addr)
    al = ctx["aliases"]
    if domain and domain in al:
        return al[domain], "别名", "这个发件人域名你归过类"
    if from_addr and from_addr.lower() in al:
        return al[from_addr.lower()], "别名", "这个发件人地址你归过类"

    hit = mail_name_hit(subject, from_name, (body or "")[:2000], ctx["names"], from_addr)
    if hit.get("sure") and hit.get("hit"):
        return hit["hit"], "名单命中", "邮件里出现了你名单里的「%s」" % hit["hit"]

    cand = _mail_company_candidate(subject, body, from_name)
    if cand:
        pool = list(ctx["recent"]) + list(ctx["names"])
        for nm in sorted(pool, key=lambda x: -len(_co_key(x))):
            if mail_same_company(cand, nm):
                return nm, "名称匹配", "邮件里的「%s」≈ 名单里的「%s」" % (cand, nm)

    if domain:
        head = domain.split(".")[0].lower()
        if len(head) >= 4:
            for nm in ctx["names"]:
                if norm_company_core(nm).lower() == head:
                    return nm, "域名匹配", "发件人域名 %s ≈ 这家公司" % domain
    return "", "", ""


def mail_match_ctx(cfg: "Config") -> Dict[str, Any]:
    """准备好「认自家公司」用的一整套字典（每轮同步算一次，不必每封邮件重算）。"""
    full, recent = known_companies(cfg)
    return {"aliases": load_mail_aliases(cfg), "names": full, "recent": recent}


def _mail_summary(body: str, n: int = 120) -> str:
    s = re.sub(r"\s+", " ", (body or "").strip())
    return s[:n]


def mail_parse_one(raw: bytes, cfg: "Config", ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一封原始邮件解析成一条结构化记录（必含 _verdict / _evidence / _fp）。

    _verdict：notice（进通知表）/ unknown（公司认得出、类型拿不准 → **照样进通知表**，类型留空）
              / unmatched（公司认不出 → 未匹配区，不进列表）/ skip（否定类）。
    """
    msg = email.message_from_bytes(raw)
    subject = _mail_decode_header(msg.get("Subject"))
    from_raw = _mail_decode_header(msg.get("From"))
    from_addr = email.utils.parseaddr(from_raw)[1] or from_raw
    from_name = email.utils.parseaddr(from_raw)[0] or from_addr
    try:
        mail_dt = parsedate_to_datetime(msg.get("Date") or "")
        if mail_dt.tzinfo is None:
            mail_dt = mail_dt.replace(tzinfo=timezone(timedelta(hours=cfg.timezone_offset)))
        mail_dt = mail_dt.astimezone(timezone(timedelta(hours=cfg.timezone_offset))).replace(tzinfo=None)
    except (TypeError, ValueError):
        mail_dt = datetime.now()

    body = mail_extract_body(msg)
    domain = _mail_domain(from_addr)
    msgid = (msg.get("Message-ID") or "").strip()
    fp = msgid or "%s|%s|%s" % (from_addr, subject, mail_dt.strftime("%Y-%m-%d %H:%M"))
    out: Dict[str, Any] = {
        "公司名称": "",
        "岗位名称": mail_extract_position(subject, body),
        "通知类型": [],
        "通知截止时间": "",
        "邮件链接": "",
        "邮件主题": subject,
        "邮件收到时间": mail_dt.strftime("%Y-%m-%d %H:%M"),
        "发件人": ("%s <%s>" % (from_name, from_addr)) if from_addr else from_name,
        "发件人域名": domain,
        "_fp": fp,
        "摘要": _mail_summary(body),
    }

    hay = (subject or "") + "\n" + (body or "")[:3000]
    hay_l = hay.lower()
    neg = [h for h in MAIL_NEGATIVE_HINTS if h.lower() in hay_l]
    if neg:
        out["_verdict"] = "skip"
        out["_evidence"] = {"为什么": "命中否定词，不是能用的通知", "命中词": neg}
        return out
    # 拒信：硬词单立 + 「感谢 + 坏消息词」成对匹配（offer 通知也满嘴"感谢"，
    # 单字匹配会把 offer 误杀 —— 两半都命中才算拒信）。
    # 命中**不再 skip**（2026-10-08 她追加：拒信别静默丢）→ verdict="reject"，
    # 往下照常认公司，后面在 mail_build_notices 里分两路。
    rej = [h for h in MAIL_REJECT_HINTS if h in hay]
    if not rej:
        for _pair_l, _pair_rs in MAIL_REJECT_PAIRS:
            if _pair_l in hay_l and any(r in hay_l for r in _pair_rs):
                rej = ["%s+%s" % (_pair_l, r) for r in _pair_rs if r in hay_l]
                break
    # Offer：必须先过拒信关 —— 「很遗憾，这次未通过」的邮件主题可能就写着「录用结果通知」。
    # 只看**主题**（2026-10-09 大修：正文里的录用词把测评邀请 / 简历回执都判成了 Offer）。
    off, off_strong = ([], False) if rej else mail_offer_hit(subject)
    special = "reject" if rej else ("offer" if off else "")

    # 先分类型（不在这儿 return，类型拿不准**不**代表这封没用），再认公司。
    # 顺序是刻意反过来的：公司认不出 → 这封属于哪家都不知道，列出来是一行没主儿的数据；
    # 公司认得出、只是类型拿不准 → 照样进列表，类型留空（见下面 unknown）。两种失败要分开。
    ntype, verdict, ev = mail_decide(subject, body, bool(domain and domain in ctx["aliases"]))
    company, how, why = mail_match_company(subject, body, from_name, from_addr, ctx)
    if not company:
        out["_verdict"] = "unmatched"
        out["_evidence"] = dict(ev, 为什么="没在你名单里认出是哪家公司"
                                     + ("（类型倒是认出来了：%s）" % ntype if ntype else ""),
                                发件人=out["发件人"], 发件人域名=domain)
        return out

    unknown = verdict != "notice"
    if unknown and not special:
        # 「待确认」闸：公司认得出≠这封是秋招邮件。主题/正文没有招聘关键词的杂邮
        # （购物、物流、会员通知碰巧含公司名）在这里挡掉，不进看板 —— 2026-10-08 她拍板。
        # 拒信 / Offer 不走这道闸 —— 拒都拒了、录都录了，本身就是求职结果，不是杂邮。
        hay2 = ((subject or "") + "\n" + (body or "")[:2000]).lower()
        recruit_hits = [h for h in MAIL_RECRUIT_HINTS if h in hay2]
        if not recruit_hits:
            out["_verdict"] = "skip"
            out["_evidence"] = dict(ev, 公司匹配=how, 匹配依据=why, 发件人=out["发件人"],
                                    为什么="公司认得出，但主题/正文没有招聘关键词，不像秋招通知，不进看板")
            return out

    deadline = mail_extract_deadline(subject + "\n" + body, mail_dt, cfg.timezone_offset, 0)
    links = mail_extract_links(msg, body)
    out.update({
        "公司名称": company,
        # 类型拿不准就留空 —— 「待确认」三个字是页面渲染角标时加的 UI 文案，不写进数据
        # 拒信 / Offer 是两个**五类之外**的既成事实类型，直接写明，别混进待确认。
        "通知类型": (["拒信"] if special == "reject" else
                    ["Offer"] if special == "offer" else
                    ([] if unknown else [ntype])),
        "通知截止时间": deadline.strftime("%Y-%m-%d %H:%M") if deadline else "",
        "邮件链接": links[0] if links else "",
        "_verdict": special or ("unknown" if unknown else "notice"),
        # 强词（录用通知/意向书/入职…）本身就是录用文书；弱词（录用/录取/offer 单独出现）
        # 推状态前要旁证 —— 见 mail_offer_corroborated。
        "_offer_strong": bool(off_strong),
        "_evidence": dict(ev, 公司匹配=how, 匹配依据=why, 发件人=out["发件人"],
                          **({"命中词": rej or off} if special else {})),
    })
    return out


# ---- 去重状态（移植 qqmail2table 的 load_state/save_state/dedupe） ----

def mail_load_state(cfg: "Config") -> Dict[str, Any]:
    path = cfg.mail_state_json
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict):
                return obj
        except (OSError, ValueError):
            pass
    return {"seen": []}


def mail_save_state(cfg: "Config", state: Dict[str, Any]) -> None:
    state["seen"] = sorted(set(state.get("seen") or []))[-3000:]   # 别让状态文件无限长
    path = cfg.mail_state_json
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---- 证据旁路 / 未匹配区 / 跳过区 ----

def mail_evidence_path(cfg: "Config") -> str:
    return os.path.join(cfg.out_dir, "notice_evidence.json")


def mail_unmatched_path(cfg: "Config") -> str:
    return os.path.join(cfg.out_dir, "mail_unmatched.csv")


def mail_skipped_path(cfg: "Config") -> str:
    return os.path.join(cfg.out_dir, "mail_skipped.jsonl")


def _json_load(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def mail_load_evidence(cfg: "Config") -> Dict[str, Any]:
    obj = _json_load(mail_evidence_path(cfg), {})
    return obj if isinstance(obj, dict) else {}


def mail_evidence_text(ev: Any) -> str:
    """把一条识别依据压成给人看的一句话，挂到通知行上（鼠标停一下就能看到「为什么标成这个类型」）。
    这是 ADR-002 要的「可解释」：不是黑盒，点得开、说得清。"""
    if not isinstance(ev, dict) or not ev:
        return ""
    parts: List[str] = []
    if ev.get("规则"):
        hits = "、".join([str(x) for x in (ev.get("命中词") or [])][:6]) or "—"
        parts.append("类型「%s」：%s命中 %s" % (ev["规则"], ev.get("位置") or "主题", hits))
    elif ev.get("为什么"):
        parts.append(str(ev["为什么"]))
    # 同分是靠优先级决出来的（比如主题里同时出现「笔试」和「测评」）—— 必须让人看见。
    # 静默决胜就又变成猜了，跟整套规则「每条判定都说得清」的底线直接冲突。
    if ev.get("同分"):
        parts.append("%s（同分者：%s）" % (ev.get("优先级") or "按优先级取",
                                     "、".join(str(x) for x in ev["同分"])))
    if ev.get("公司匹配"):
        parts.append("公司按「%s」对上：%s" % (ev["公司匹配"], ev.get("匹配依据") or ""))
    return "；".join([p for p in parts if p.strip("； ")])


def mail_attach_evidence(cfg: "Config", notices: List[Dict[str, Any]]) -> int:
    """给每条通知挂一句「凭什么这么标」（就地改 notices）。只有邮箱直连收来的才有依据。"""
    ev = mail_load_evidence(cfg)
    if not ev:
        return 0
    hit = 0
    for n in notices:
        txt = mail_evidence_text(ev.get(str(n.get("_key") or "")))
        if txt:
            n["依据"] = txt
            hit += 1
    return hit


def mail_read_csv(path: str) -> List[Dict[str, str]]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [dict(r) for r in csv.DictReader(f)]
    except OSError:
        return []


def mail_write_csv(path: str, fields: List[str], rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    os.replace(tmp, path)


def mail_existing_notice_rows(cfg: "Config") -> List[Dict[str, Any]]:
    """把现有 notices.csv 读回来（保留已经攒下来的历史通知）。通知类型统一成 list，
    否则 write_notices_csv 再拼「、」时会把「面试」拆成「面、试」。"""
    path = cfg.notices_csv
    if not path or not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if not (r.get("公司名称") or "").strip():
                    continue
                d = dict(r)
                d["通知类型"] = [t for t in (d.get("通知类型") or "").replace("，", "、").split("、") if t]
                rows.append(d)
    except OSError:
        return []
    return rows


def mail_row_key(r: Dict[str, Any]) -> Tuple[str, str, str, str]:
    ts = r.get("通知类型")
    if isinstance(ts, list):
        ts = "、".join(ts)
    return (str(r.get("公司名称", "")).strip(), str(ts or "").strip(),
            str(r.get("通知截止时间", "")).strip(), str(r.get("邮件主题", "")).strip())


def mail_app_hits(apps: List[Dict[str, Any]], company: str,
                  position: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """找这家公司的投递记录；岗位名称对得上就精确到岗位。
    返回 (全部命中, 精确命中)。这是邮件结果 ↔ 投递记录的**唯一**认人口径。

    ⚠️ 2026-10-09：口径从 `same_company`（裸子串）换成 `mail_same_company`（子串要求短名 ≥3 字），
    否则「京东」会并进「京东方」。没对上的邮件不会丢 —— 走通知表那一路，可见、可被她点归类。
    """
    hits = [a for a in apps if mail_same_company(company, str(a.get("公司名称") or ""))]
    pos = (position or "").strip()
    exact = ([a for a in hits if str(a.get("岗位名称") or "").strip() == pos]
             if pos and hits else [])
    return hits, exact


def mail_evidence_of(p: Dict[str, Any]) -> Dict[str, str]:
    """一封拒信 / Offer 邮件的「依据」：进度行上至少要留主题或链接一样（她 2026-10-08 要的）。"""
    ev = {"类型": "拒信" if p.get("_verdict") == "reject" else "Offer",
          "主题": str(p.get("邮件主题") or "").strip(),
          "链接": str(p.get("邮件链接") or "").strip(),
          "时间": str(p.get("邮件收到时间") or "").strip()}
    return {k: v for k, v in ev.items() if v}


def mail_offer_push_ok(row: Dict[str, Any]) -> bool:
    """Offer 状态能不能由邮件自动推上去 —— **她手改过的状态绝不覆盖**（铁律）。

    与飞书同步的冲突规则同口径（merge_apps：状态偏离「飞书状态」基线 = 有人手动动过）：
    · 有飞书状态基线 → 只有 状态 == 基线（她没手改）才推；
    · 没有基线的本地行 → 只有还是默认「已投递」才推（别的状态都是她/导入有意写的）；
    · 已经是 Offer / 已结束的照旧不推（已结束更不许翻案）。
    """
    st = norm_status(str(row.get("状态") or ""))
    if st in OFFER_STATUSES or st == "已结束":
        return False
    prev = str(row.get("飞书状态") or "").strip()
    if prev:
        return st == norm_status(prev)
    return st == "已投递"


def mail_offer_corroborated(cfg: "Config", company: str, p: Dict[str, Any]) -> bool:
    """这封"录用"邮件有没有旁证 —— 她 2026-10-09 点名的逻辑：

    「如果是从邮箱抓的、我真收到 offer 了，那前面几封这个公司的面试通知邮件也该有；
     一封都没有，你就直接判我被录用了？」

    · 强词（录用通知 / 意向书 / 入职 / 报到 / 三方协议 …）本身就是录用文书 → 直接放行；
    · 弱词（录用 / 录取 / offer 单独出现）→ 必须先看到这家有测评 / 笔试 / AI面 / 面试类通知。
    读不出通知表时**放行**（少一次误判优先，且依据照样挂在她行上看得见）。
    """
    if p.get("_offer_strong"):
        return True
    when = str(p.get("邮件收到时间") or "").strip()
    try:
        rows = load_notices(cfg)
    except Exception:
        return True
    for r in rows:
        if not mail_same_company(company, str(r.get("公司名称") or "")):
            continue
        types = r.get("通知类型") or []
        if isinstance(types, str):
            types = [t for t in types.replace("，", "、").split("、") if t]
        if not any(t in MAIL_CORROB_TYPES for t in types):
            continue
        rt = str(r.get("邮件收到时间") or "").strip()
        if not when or not rt or rt <= when:
            return True
    return False


def mail_apply_verdict(cfg: "Config", p: Dict[str, Any]) -> bool:
    """拒信 / Offer 邮件落到「投递进度」（2026-10-08 她追加：拒信别静默丢）。

    匹配到投递记录 → 把结果记在 interview_progress.json（**CSV 列一列不动**，ADR-005；
    手改状态不覆盖的铁律在 mail_offer_push_ok 里把着）：
      · 拒信：这家「打完这封就没有别的活口」时整家标止步（止步来源=邮件）——
        岗位精确对上时以那个岗位为准；还有别的岗在跑就**不一刀切**（她钉过
        「一家多岗只挂一岗不算整家止步」），依据照挂，止步让她自己点。
      · Offer：状态推到「Offer」（能过 guard 的话）；依据两种情况都挂行上。
    匹配不到任何投递记录 → 返回 False，调用方把邮件放进通知表（类型「拒信」/「Offer」），
    可见、可被同公司归纳 —— 同样不静默丢。
    """
    apps = load_apps(cfg)
    hits, exact = mail_app_hits(apps, str(p.get("公司名称") or ""), str(p.get("岗位名称") or ""))
    if not hits:
        return False
    # 进度键跟着**她投递记录里的公司名**走（不是名单原名）—— 同一家才并成一行
    key_name = str(hits[0].get("公司名称") or "").strip() or str(p.get("公司名称") or "")
    key = interview_key(key_name)
    if not key:
        return False
    ev = mail_evidence_of(p)
    if p.get("_verdict") == "reject":
        live = [a for a in hits if norm_status(str(a.get("状态") or "")) != "已结束"]
        if exact:
            target_dead = norm_status(str(exact[0].get("状态") or "")) == "已结束"
            stop = (not target_dead) and len(live) <= 1
        else:
            stop = len(live) == 1
        set_interview_progress(cfg, key, company=key_name,
                               stop=True if stop else None, stop_src="邮件", ev=ev)
        return True
    # Offer：依据先挂上（不管状态推不推得动，这封邮件都得看得见）
    set_interview_progress(cfg, key, company=key_name, ev=ev)
    targets = exact or [a for a in hits
                        if norm_status(str(a.get("状态") or "")) not in ("Offer", "已结束")]
    if (len(targets) == 1 and mail_offer_push_ok(targets[0])
            and mail_offer_corroborated(cfg, key_name, p)):
        update_app(cfg, str(targets[0].get("id") or ""), 状态="Offer")
    return True


def mail_build_notices(cfg: "Config", raws: List[bytes]) -> Dict[str, int]:
    """把一批原始邮件变成 / 合并进 notices.csv，并把旁路文件写掉。返回一堆计数（供提示用）。

    · notices.csv：**追加 + 去重**（历史通知留着；同一封邮件不重复进）。
      「已进过通知表」的邮件指纹记在 .mail_state.json —— 因为每次回扫都覆盖最近 days_back，
      不记就会每轮重复处理（而且改规则后截止时间变了，内容键也挡不住）。
    · mail_unmatched.csv：**每轮重算**（是待办清单，不是日志）——
      用户给未匹配邮件归类后，下一轮同域名的邮件就该自动对上，不该被旧记录钉死。
    · mail_skipped.jsonl：追加日志，按指纹去重，最多留 2000 行。
    · notice_evidence.json：键 = notice_state_key，随通知表一起收敛（不再存在的键会清掉）。
    """
    ctx = mail_match_ctx(cfg)
    st = mail_load_state(cfg)
    seen = set(st.get("seen") or [])
    rows = mail_existing_notice_rows(cfg)
    have = {mail_row_key(r) for r in rows}
    evidence = mail_load_evidence(cfg)

    unmatched: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    added = 0
    unknown = 0
    applied = 0
    for raw in raws:
        p = mail_parse_one(raw, cfg, ctx)
        if not p:
            continue
        v = p["_verdict"]
        if v == "skip":
            skipped.append(p)
            continue
        if v == "unmatched":
            unmatched.append(p)
            continue
        # 拒信 / Offer（2026-10-08 她追加：这两类不许静默丢）分两路：
        # 匹配到投递记录 → 落投递进度（止步 / 状态推 Offer，依据挂行上）；
        # 匹配不到 → 进通知表（类型「拒信」/「Offer」），可见、可被同公司归纳。
        # 指纹照样记 seen —— 回扫别把同一场结果再写一遍。
        if v in ("reject", "offer"):
            seen.add(p["_fp"])
            if mail_apply_verdict(cfg, p):
                applied += 1
            else:
                key = notice_state_key(p)
                evidence[key] = dict(p["_evidence"], 邮件主题=p["邮件主题"],
                                     收到时间=p["邮件收到时间"])
                rows.append(p)
                have.add(mail_row_key(p))
                added += 1
                p["_key"] = key
            continue
        # notice（类型认出来了）和 unknown（公司认得出、类型拿不准）**都进通知表**。
        # unknown 那条类型留空，页面给它打「待确认」角标，点一下就能选类型 ——
        # 早先这些落 data/mail_review.csv，拿到发行版的人根本不会想起来去翻一个 CSV。
        if p["_fp"] in seen:
            continue
        k = mail_row_key(p)
        if k in have:
            seen.add(p["_fp"])
            continue
        # 催办去重（规则源：作者的邮箱→飞书自动化，移植进直连引擎）：
        # 类型认得出、主题命中「提醒/催办/再次/未完成/尽快完成/截止在即」、
        # 且**同公司 + 同类型 + 截止时间相同（或双方都没有截止）**已有记录
        # （判定口径 = 本轮累计，含本轮先写进 CSV 的）→ 这封只是催，不另立一行，
        # 证据写明「对已有记录的重复催办」。
        # ⚠️ 截止时间不同**不算**同一件事的催 —— 那是改期 / 重新安排的测评，
        # 新截止时间必须进面板（2026-10-08 她拍板：「不同截止时间的不要跳过」）。
        # 类型拿不准的（unknown）不参与 —— 没有类型可比，这种进表是「待确认」让人自己定。
        bump = [h for h in MAIL_BUMP_HINTS if h in (p["邮件主题"] or "")]
        if bump and p.get("通知类型"):
            dup = any(r.get("公司名称", "") == p.get("公司名称", "") and
                      set(r.get("通知类型") or []) & set(p["通知类型"]) and
                      str(r.get("通知截止时间", "")).strip() ==
                      str(p.get("通知截止时间", "")).strip()
                      for r in rows)
            if dup:
                p["_verdict"] = "skip"
                p["_evidence"] = dict(p.get("_evidence") or {},
                                      为什么="对已有记录的重复催办（主题命中：%s）" % "、".join(bump))
                skipped.append(p)
                seen.add(p["_fp"])
                continue
        key = notice_state_key(p)
        evidence[key] = dict(p["_evidence"], 邮件主题=p["邮件主题"], 收到时间=p["邮件收到时间"])
        rows.append(p)
        have.add(k)
        seen.add(p["_fp"])
        added += 1
        if v == "unknown":
            unknown += 1
        p["_key"] = key

    write_notices_csv(cfg, rows)
    st["seen"] = sorted(seen)
    mail_save_state(cfg, st)

    # 证据只保留当前通知表里还存在的那些（不然会无限长）
    live = {notice_state_key({"公司名称": r.get("公司名称", ""), "通知类型": r.get("通知类型") or [],
                              "通知截止时间": r.get("通知截止时间", ""),
                              "邮件主题": r.get("邮件主题", "")}) for r in rows}
    evidence = {k: v for k, v in evidence.items() if k in live}
    _json_atomic(mail_evidence_path(cfg), evidence)

    mail_write_csv(mail_unmatched_path(cfg), MAIL_UNMATCHED_FIELDS, unmatched)
    _mail_write_skipped(cfg, skipped)

    return {"notices": len(rows), "added": added, "unknown": unknown,
            "applied": applied, "unmatched": len(unmatched), "skipped": len(skipped)}


def _json_atomic(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _mail_write_skipped(cfg: "Config", rows: List[Dict[str, Any]]) -> None:
    path = mail_skipped_path(cfg)
    old: List[Dict[str, Any]] = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(o, dict):
                        old.append(o)
        except OSError:
            old = []
    seen = {str(o.get("_fp") or "") for o in old}
    out = list(old)
    for r in rows:
        if r["_fp"] in seen:
            continue
        out.append({"_fp": r["_fp"], "邮件收到时间": r["邮件收到时间"], "发件人": r["发件人"],
                    "邮件主题": r["邮件主题"],
                    "命中词": (r.get("_evidence") or {}).get("命中词") or [],
                    "为什么": (r.get("_evidence") or {}).get("为什么") or ""})
        seen.add(r["_fp"])
    out = out[-2000:]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


# ---- 连邮箱取信（唯一的联网部分） ----

def _mail_utf7_encode(s: str) -> str:
    """IMAP 的 modified UTF-7，用于中文文件夹名。"""
    out: List[str] = []
    buf = ""
    for ch in s:
        if "\x20" <= ch <= "\x7e":
            if buf:
                out.append(_mail_b64_utf7(buf))
                buf = ""
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    if buf:
        out.append(_mail_b64_utf7(buf))
    return "".join(out)


def _mail_b64_utf7(s: str) -> str:
    return "&" + base64.b64encode(s.encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-"


_MAIL_MAX_BYTES = 1024 * 1024   # 超过 1MB 的邮件只抓头：正文几乎全是附件，识别用不上


def mail_since_criteria(days_back: int, only_unseen: bool, now=None) -> List[str]:
    """IMAP 搜索条件。**关键词分开传，绝不裹在括号里**（2026-10-09 修的实锤）。

    原写法 `imap.search(None, "(SINCE 09-Sep-2026)")` 在 QQ 邮箱上**条件被直接忽略**，
    返回全库 —— 实测同一时刻：带括号 1853 封（全部），分开传 368 封（正确）。
    后果不只是慢：每 6 小时把 3 年的邮件全跑一遍，2024 年 Stardock 的游戏广告信
    就是这么上了她的秋招看板、还把 vivo 那行推成了 Offer。
    """
    t = now or datetime.now()
    since = (t - timedelta(days=max(1, days_back))).strftime("%d-%b-%Y")
    return (["UNSEEN", "SINCE", since] if only_unseen else ["SINCE", since])


def _mail_post_login(imap, cfg: "Config") -> None:
    """有些邮箱（163 / 126）登录后要先发一条 ID 命令，否则报 Unsafe Login。
    这是个 best-effort：不支持就算了，别为了它把整次同步搞挂（ADR-001）。"""
    host = (cfg.mail_host or "").lower()
    if "163" not in host and "126" not in host:
        return
    try:
        imap.xatom("ID", '("name" "applytracker" "version" "%s")' % APP_VERSION)
    except Exception:
        pass


# 页面直连向导用：按邮箱域名推断 IMAP 服务器。推断不出就让用户手填，**绝不瞎猜**。
MAIL_HOST_MAP: Dict[str, Tuple[str, int]] = {
    "qq.com": ("imap.qq.com", 993), "foxmail.com": ("imap.qq.com", 993),
    "163.com": ("imap.163.com", 993), "126.com": ("imap.126.com", 993),
    "gmail.com": ("imap.gmail.com", 993),
    "outlook.com": ("outlook.office365.com", 993),
    "hotmail.com": ("outlook.office365.com", 993), "live.com": ("outlook.office365.com", 993),
}


def mail_infer_server(email: str) -> Tuple[str, int]:
    """按邮箱地址的域名推断 (IMAP 服务器, 端口)。认不出返回 ("", 0)。"""
    dom = (email or "").rsplit("@", 1)[-1].strip().lower()
    hit = MAIL_HOST_MAP.get(dom)
    return (hit[0], hit[1]) if hit else ("", 0)


def mail_test_login(host: str, port: int, user: str, authcode: str) -> Tuple[bool, str]:
    """当场验一次登录，给向导用。不写入任何东西。"""
    try:
        with imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30) as imap:
            imap.login(user, authcode)
            imap.select("INBOX", readonly=True)
        return True, ""
    except imaplib.IMAP4.error as e:
        return False, str(e)
    except OSError as e:
        return False, "网络问题：%s" % e


def mail_fetch(cfg: "Config") -> List[bytes]:
    """连邮箱，把最近 days_back 天里的邮件原始字节拉回来。只读、不标已读。

    必须批量抓 + 设超时（2026-10-08 真实邮箱翻车的教训）：真实收件箱 30 天
    能有 1800+ 封，一封一封抓要按小时算；中间碰上服务器一次迟疑，线程就
    永远挂在 fetch 上、同步锁永远不释放，看板从此再也收不了邮件。
    三道保险：
      1) IMAP4_SSL(timeout=60)：任何一步卡 60 秒就报错，锁正常释放；
      2) 大小先一批拿回，>1MB 的只抓邮件头（附件型大邮件的正文对识别
         公司/类型没用，一封 19MB 能拖 45 秒以上）；
      3) 小邮件 50 封一批抓全文，网络往返从 1800+ 次压到 ~40 次。
    """
    if not cfg.mail_configured:
        raise RuntimeError("还没配邮箱：请在 config.ini 的 [mail] 里填 user（邮箱）和 authcode（IMAP 授权码）")
    out: List[bytes] = []
    ctx = ssl.create_default_context()
    try:
        conn = imaplib.IMAP4_SSL(cfg.mail_host, cfg.mail_port, ssl_context=ctx, timeout=60)
    except OSError as e:
        raise RuntimeError("连不上 %s:%s（%s）—— 看看网络，或 [mail] 的 host / port 填错没有"
                           % (cfg.mail_host, cfg.mail_port, e))
    with conn as imap:
        try:
            imap.login(cfg.mail_user, cfg.mail_authcode)
        except imaplib.IMAP4.error as e:
            raise RuntimeError(
                "登录失败：%s\n"
                "  1) QQ邮箱网页版 → 设置 → 账户 → 开启 IMAP/SMTP 服务\n"
                "  2) 用发短信后生成的 16 位授权码，不是登录密码\n"
                "  3) 授权码刚生成就登录，它只在弹窗里出现一次" % e)
        _mail_post_login(imap, cfg)
        folder = cfg.mail_folder
        if not folder.isascii():
            folder = _mail_utf7_encode(folder)     # 中文文件夹名要用 modified UTF-7
        typ, _ = imap.select('"%s"' % folder, readonly=True)   # 只读：不把邮件标成已读
        if typ != "OK":
            raise RuntimeError("打不开文件夹 %s" % cfg.mail_folder)
        # 条件必须分开传 —— 裹成 "(SINCE 09-Sep-2026)" 一个字符串会被服务器忽略、返回全库
        typ, data = imap.search(None, *mail_since_criteria(cfg.mail_days_back,
                                                          cfg.mail_only_unseen))
        if typ != "OK":
            return []
        seqs = [s.decode() for s in data[0].split()]

        def _fetch_batch(ids: List[str], what: str, chunk: int) -> List[bytes]:
            got: List[bytes] = []
            for i in range(0, len(ids), chunk):
                typ, d2 = imap.fetch(",".join(ids[i:i + chunk]), what)
                if typ != "OK":
                    continue
                for part in d2:
                    if isinstance(part, tuple):
                        got.append(part[1])
            return got

        # 第一批命令：全部邮件的大小（每批 500 个序号，一次命令就回）
        sizes: Dict[str, int] = {}
        for i in range(0, len(seqs), 500):
            typ, d2 = imap.fetch(",".join(seqs[i:i + 500]), "(RFC822.SIZE)")
            if typ != "OK":
                continue
            for part in d2:
                if isinstance(part, tuple):
                    continue
                m = re.match(rb"(\d+) \(RFC822\.SIZE (\d+)\)", part)
                if m:
                    sizes[m.group(1).decode()] = int(m.group(2))
        big = [s for s in seqs if sizes.get(s, _MAIL_MAX_BYTES + 1) > _MAIL_MAX_BYTES]
        small = [s for s in seqs if s not in big]
        out += _fetch_batch(small, "(RFC822)", 50)          # 小邮件抓全文（正文参与识别）
        out += _fetch_batch(big, "(RFC822.HEADER)", 100)    # 大邮件只抓头（主题/发件人够识别）
    return out


def mail_load_eml(path: str) -> List[bytes]:
    """不连邮箱，直接读本地 .eml / .txt 文件 —— 离线调规则用（--mail-eml）。"""
    files: List[str] = []
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
            files += [os.path.join(root, n) for n in names
                      if n.lower().endswith((".eml", ".txt"))]
        files.sort()
    elif os.path.exists(path):
        files = [path]
    out: List[bytes] = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                out.append(fh.read())
        except OSError as e:
            print("  ! 读不了 %s：%s" % (f, e))
    return out


def mail_unmatched_rows(cfg: "Config") -> List[Dict[str, str]]:
    return mail_read_csv(mail_unmatched_path(cfg))


# ---- 配置向导 / 离线自查 / 命令行 ----

def _ask_secret(prompt: str) -> str:
    """敏感输入（授权码）。终端里跑就隐藏输入；被管道喂输入时直接读一行，方便自动化，不至于卡住。"""
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass(prompt).strip()
    sys.stdout.write(prompt + "\n")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def _ini_set_text(text: str, section: str, key: str, value: str) -> str:
    """在 ini 文本里就地改 `[section]` 下的 `key = value`：键在就改，不在就在该段末尾补一行，
    段都没有就整段追加。**尽量保住别的注释与顺序** —— config.ini 是从带注释的示例拷来的，
    用 configparser 写回会把注释全抹掉。"""
    lines = text.splitlines()
    sec_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    key_re = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    start = -1
    end = len(lines)
    for i, ln in enumerate(lines):
        m = sec_re.match(ln)
        if not m:
            continue
        if start < 0 and m.group(1).strip() == section:
            start = i
        elif start >= 0:
            end = i
            break
    if start < 0:
        return text.rstrip("\n") + "\n\n[%s]\n%s = %s\n" % (section, key, value)
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            lines[i] = "%s = %s" % (key, value)
            break
    else:
        lines.insert(end, "%s = %s" % (key, value))
    return "\n".join(lines) + "\n"


def mail_config_write(path: str, values: Dict[str, str]) -> str:
    """把 [mail] 的值写回 config.ini（保住注释），并把文件权限收到 600（授权码在里面）。"""
    text = ""
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
    for k, v in values.items():
        text = _ini_set_text(text, "mail", k, v)
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)      # 授权码存这儿，别让同机别的账号读到
        except OSError:
            pass
    return path


def _ini_read(path: str, section: str, key: str, default: str = "") -> str:
    """只读 config.ini 里的一个键（不改盘）。读不到 / 读坏了就给默认值。"""
    try:
        cp = configparser.ConfigParser()
        cp.read(path, encoding="utf-8")
        return (cp.get(section, key) or "").strip() or default
    except Exception:
        return default


def ini_patch(path: str, changes: List[Tuple[str, str, str]]) -> None:
    """就地改 config.ini 里的几个键（段落 / 注释 / 顺序都尽量保住），一次写完。

    changes = [(section, key, value), ...]。走 tmp + os.replace，跟 mail_config_write
    一个路子 —— 半截写坏会把别的段一起丢。path 为空就当没写（测试里传空路径）。"""
    text = ""
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
    for section, key, value in changes:
        text = _ini_set_text(text, section, key, value)
    if not path:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def run_mail_wizard(cfg: Config, config_path: str = "") -> int:
    """--mail-config：收邮箱 + 授权码，当场 test_login，通过就写回 config.ini（ADR-001）。"""
    path = config_path or cfg.path or "config.ini"
    print("=" * 58)
    print("  连你的邮箱（IMAP）—— 让看板自己去收校招通知")
    print("=" * 58)
    print("\n需要一样东西：QQ 邮箱的 IMAP 授权码（16 位，**不是**登录密码）")
    print("  1) 浏览器打开 https://mail.qq.com 登录")
    print("  2) 设置 → 账户 → 找到「IMAP/SMTP服务」→ 开启")
    print("  3) 手机发一条短信验证，弹窗里会给出授权码（只出现一次，关掉要重新生成）")
    print("\n（163/126 去「设置 → POP3/SMTP/IMAP」开，用「客户端授权密码」；")
    print("  Gmail 要先开两步验证再生成「应用专用密码」；学校邮箱问学校 IT。）\n")

    def ask(prompt: str, default: str) -> str:
        try:
            v = input("%s [%s] " % (prompt, default) if default else prompt + " ").strip()
        except (EOFError, KeyboardInterrupt):
            return default
        return v or default

    host = ask("IMAP 服务器：", cfg.mail_host or "imap.qq.com")
    port_s = ask("端口：", str(cfg.mail_port or 993))
    try:
        port = int(port_s)
    except ValueError:
        port = 993
    user = ask("邮箱地址：", cfg.mail_user or "")
    if not user:
        print("✗ 邮箱地址没填，先不写了。")
        return 1
    authcode = _ask_secret("授权码（输入时不显示）：") or cfg.mail_authcode
    if not authcode:
        print("✗ 授权码没填，先不写了。")
        return 1

    print("\n→ 正在试登录 %s:%s …" % (host, port))
    ok, err = mail_test_login(host, port, user, authcode)
    if ok:
        print("✓ 登录成功。")
    else:
        print("✗ 登录失败：%s" % err)
        try:
            if input("还是要先存下来吗？[y/N] ").strip().lower() not in ("y", "yes"):
                return 1
        except (EOFError, KeyboardInterrupt):
            return 1

    values = {"source": "auto", "host": host, "port": str(port), "user": user,
              "authcode": authcode, "folder": cfg.mail_folder or "INBOX",
              "days_back": str(cfg.mail_days_back or 30),
              "only_unseen": "true" if cfg.mail_only_unseen else "false",
              "ttl": str(cfg.mail_ttl or 900)}
    mail_config_write(path, values)
    print("✓ 已写入 %s（权限 600）。下次启动（或 --notices-sync）就会去收邮件。" % path)
    print("  不想连了就把它删掉，或把 [mail] source 改成 off。")
    return 0


def mail_eml_report(cfg: Config, path: str) -> int:
    """--mail-eml：**不联网**，拿本地 .eml 文件把识别规则跑一遍，逐封打印判定与依据。
    调规则 / 验收都用它（ARCH §10 落地第 4 步：先离线，再连线）。"""
    raws = mail_load_eml(path)
    if not raws:
        print("✗ 没找到 .eml / .txt 文件：%s" % path)
        return 1
    ctx = mail_match_ctx(cfg)
    stat = {"notice": 0, "unknown": 0, "unmatched": 0, "skip": 0}
    print("· 拿 %d 封邮件跑规则（不连邮箱）\n" % len(raws))
    for raw in raws:
        p = mail_parse_one(raw, cfg, ctx)
        if not p:
            print("  · （解析不出内容，跳过）")
            continue
        v = p["_verdict"]
        stat[v] = stat.get(v, 0) + 1
        # .get(v, v)：以后再加新结论时这里是打印出原文，而不是整个命令崩掉
        tag = {"notice": "通知", "unknown": "待确认（照样进列表）",
               "unmatched": "没认出公司", "skip": "跳过"}.get(v, v)
        kinds = "、".join(p.get("通知类型") or []) or "—"
        print("  [%s] %s" % (tag, p["邮件主题"] or "（无主题）"))
        print("        发件人：%s" % p["发件人"])
        if v in ("notice", "unknown"):
            print("        公司：%s   类型：%s   截止：%s"
                  % (p["公司名称"], kinds if v == "notice" else "待确认",
                     p["通知截止时间"] or "（未识别）"))
        else:
            ev = p.get("_evidence") or {}
            print("        依据：%s" % (ev.get("为什么") or ev.get("并列") or ev))
    print("\n· 合计：通知 %d，类型待确认 %d（照样进了列表），没认出公司 %d，跳过 %d"
          % (stat["notice"], stat["unknown"], stat["unmatched"], stat["skip"]))
    return 0


# ---------------------------------------------------------------------------
# 面试进度（每家公司一条：这家实际有哪些环节 + 现在走到哪一环）
# ---------------------------------------------------------------------------
# 为什么单独做一份：邮箱里来的「测评 / AI面 / 面试」通知只说了「有这件事」，
# 说不清「走到第几轮」；而且每家的流程都不一样（有的加群面、有的四轮、有的没有 AI 面）。
# 所以进度条不是写死的一条，而是每家一份：
#   节点 = 这家公司实际的环节顺序（默认 测评 / AI面试 / 一面 / 二面 / 终面 / offer，能改）
#   当前 = 已经走到哪一环（没手改过就按通知类型 + 邮件主题自动推断）
# 手改过的以手改为准，随时能「恢复自动」。落盘在 data/interview_progress.json。

# 默认阶梯：她说的一般情况
INTERVIEW_LADDER = ["测评", "AI面试", "一面", "二面", "终面", "offer"]

# 环节的标准次序。推断出来的新环节（笔试 / 群面 / 三面 / HR面…）按这个插进阶梯，
# 认不出的环节留在 offer 前面，offer 永远压最后。
INTERVIEW_ORDER = ["测评", "笔试", "AI面试", "群面", "一面", "二面", "三面", "终面", "HR面", "offer"]

# 邮件主题里明写的轮次 —— 比「通知类型」准：类型只有「面试」两个字，主题常写着「一面」「复试」。
# 顺序有讲究：先认最具体的（AI面 / 终面），再认泛的（一面）。
INTERVIEW_ROUND_PAT = [
    (r"ai\s*面", "AI面试"),
    (r"终面|终试|final", "终面"),
    (r"三面|三次面试", "三面"),
    (r"二面|复试|复面|二次面试", "二面"),
    (r"hr\s*面|人力面", "HR面"),
    (r"群面|无领导", "群面"),
    (r"一面|初面|初试|初次面试", "一面"),
]

# 投递记录里的状态 → 至少走到哪一环（她那张表里只有这几个粗粒度状态）
INTERVIEW_FROM_STATUS = {"测评笔试": "测评", "面试": "一面", "Offer": "offer"}

INTERVIEW_MAX_NODES = 12      # 一家最多几个环节，防手滑


def interview_key(name: str) -> str:
    """面试进度的存储键：跟投递记录、通知配对共用同一套公司名归一。"""
    return norm_company(name)


def interview_path(cfg: "Config") -> str:
    return os.path.join(cfg.out_dir, "interview_progress.json")


def _iv_clean_nodes(nodes: Any) -> List[str]:
    """节点清洗：去空、去重、限长。坏数据当没给，不抛。"""
    if not isinstance(nodes, (list, tuple)):
        return []
    out: List[str] = []
    for n in nodes:
        s = str(n or "").strip()[:20]
        if s and s not in out:
            out.append(s)
        if len(out) >= INTERVIEW_MAX_NODES:
            break
    return out


def _iv_sort_nodes(nodes: List[str]) -> List[str]:
    """按标准次序排一遍，保证「测评在前、offer 在最后」不会因为她手填的顺序乱掉。"""
    known = [n for n in INTERVIEW_ORDER if n in nodes and n != "offer"]
    extra = [n for n in nodes if n not in INTERVIEW_ORDER]
    out = known + extra
    if "offer" in nodes:
        out.append("offer")
    return out


def _iv_rank(node: str) -> int:
    return INTERVIEW_ORDER.index(node) if node in INTERVIEW_ORDER else len(INTERVIEW_ORDER)


def load_interview_progress(cfg: "Config") -> Dict[str, Dict[str, Any]]:
    """读本地那份手动的面试进度。文件坏了当没有 —— 看板不能因为一个坏文件打不开。"""
    path = interview_path(cfg)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        rec: Dict[str, Any] = {"节点": _iv_clean_nodes(v.get("节点"))}
        cur = str(v.get("当前") or "").strip()[:20]
        if cur:
            rec["当前"] = cur
        if v.get("止步"):
            rec["止步"] = True
        src = str(v.get("止步来源") or "").strip()[:20]
        if src:
            rec["止步来源"] = src
        ev = v.get("邮件依据")
        if isinstance(ev, dict):
            ev2 = {kk: str(ev.get(kk) or "").strip()[:500]
                   for kk in ("类型", "主题", "链接", "时间")}
            if any(ev2.values()):
                rec["邮件依据"] = ev2
        if isinstance(v.get("公司"), str):
            rec["公司"] = v["公司"]
        rec["更新"] = str(v.get("更新") or "")
        # 有「邮件依据」的也算活记录 —— 拒信 / Offer 邮件落在进度行上，别读回来就丢
        if rec["节点"] or rec.get("当前") or rec.get("止步") or rec.get("邮件依据"):
            out[k] = rec
    return out


def set_interview_progress(cfg: "Config", key: str, company: str = "",
                           nodes: Any = None, cur: Any = None,
                           stop: Any = None, reset: bool = False,
                           stop_src: Any = None, ev: Any = None) -> bool:
    """写一家公司的面试进度。各字段各改各的：
    nodes=换环节顺序 / cur=改「现在走到哪」/ stop=标止步（stop_src=止步来源，
    默认「手动」；邮件拒信自动标的传「邮件」）/ reset=删掉手动记录、回到自动 /
    ev=挂一条「邮件依据」（拒信 / Offer 邮件的主题·链接，进度行上看得见凭据）。"""
    key = str(key or "").strip()
    if not key:
        return False
    obj = load_interview_progress(cfg)      # 先原样读回来，别把别家的记录冲掉
    if reset:
        obj.pop(key, None)
    else:
        rec = obj.get(key) or {"节点": []}
        if company:
            rec["公司"] = str(company).strip()[:80]
        if nodes is not None:
            clean = _iv_clean_nodes(nodes)
            if not clean:
                return False
            # 她按什么顺序填都行，落盘前统一按标准次序摆好 —— 后面读出来的次序才稳
            rec["节点"] = _iv_sort_nodes(clean)
        if cur is not None:
            c = str(cur).strip()[:20]
            # 手动指到一个阶梯上还没有的环节：补进去，别把她的输入丢了
            if c and c not in (rec.get("节点") or []):
                rec["节点"] = _iv_sort_nodes(list(rec.get("节点") or []) + [c])
            rec["当前"] = c
        if stop is not None:
            if stop:
                rec["止步"] = True
                rec["止步来源"] = str(stop_src or "").strip()[:20] or "手动"
            else:
                rec.pop("止步", None)
                rec.pop("止步来源", None)
        if isinstance(ev, dict):
            ev2 = {kk: str(ev.get(kk) or "").strip()[:500]
                   for kk in ("类型", "主题", "链接", "时间")}
            if any(ev2.values()):
                rec["邮件依据"] = ev2
        rec["更新"] = now_str()
        obj[key] = rec
    path = interview_path(cfg)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return True


def interview_stage_of(n: Dict[str, Any]) -> Tuple[str, bool]:
    """一条通知落在阶梯的哪一环。返回 (环节, 「知道有面试但不知道第几轮」)。
    先看邮件主题里明写的轮次，主题没说再退回通知类型。"""
    subject = (str(n.get("邮件主题") or "") + " " + str(n.get("岗位名称") or "")).lower()
    for pat, node in INTERVIEW_ROUND_PAT:
        if re.search(pat, subject):
            return node, False
    types = "、".join(n.get("通知类型") or [])
    low = types.lower()
    if "ai面" in low:
        return "AI面试", False
    if "面试" in types:
        return "一面", True     # 只知道有面试，第几轮看不出来 → 让她点一下确认
    if "笔试" in types:
        return "笔试", False
    if "测评" in types:
        return "测评", False
    if "offer" in low or "录用" in types or "意向书" in types:
        return "offer", False
    return "", False


def derive_interview(cfg: "Config", notices: List[Dict[str, Any]],
                     apps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """通知 + 投递状态揉成「每家公司的面试进度」。

    只有「收到过测评 / 面试类通知」或「投递状态已经到测评笔试 / 面试 / Offer」的公司才进来 ——
    还在简历筛选阶段的没必要占这块地方。
    """
    saved = load_interview_progress(cfg)
    groups: Dict[str, Dict[str, Any]] = {}

    def g_of(name: str) -> Dict[str, Any]:
        key = interview_key(name)
        g = groups.get(key)
        if g is None:
            g = {"公司": name, "岗位": [], "类型": [], "节点": [], "hit": [], "通知数": 0, "最近": ""}
            groups[key] = g
        elif len(name) > len(g["公司"]):
            g["公司"] = name        # 存最全的那个名字（"云溪智能（杭州）网络有限公司" 比 "云溪智能" 强）
        return g

    for n in notices:
        name = str(n.get("公司名称") or "").strip()
        if not name or not interview_key(name):
            continue
        g = g_of(name)
        g["通知数"] += 1
        got = str(n.get("邮件收到时间") or "")
        if got > g["最近"]:
            g["最近"] = got
        pos = str(n.get("岗位名称") or "").strip()
        if pos and pos not in g["岗位"]:
            g["岗位"].append(pos)
        for t in (n.get("通知类型") or []):
            if t not in g["类型"]:
                g["类型"].append(t)
        node, unsure = interview_stage_of(n)
        if node:
            if node not in g["节点"]:
                g["节点"].append(node)
            g["hit"].append((node, unsure))

    for a in apps:
        name = str(a.get("公司名称") or "").strip()
        if not name or not interview_key(name):
            continue
        node = INTERVIEW_FROM_STATUS.get(str(a.get("状态") or "").strip())
        if not node:
            continue
        g = g_of(name)
        pos = str(a.get("岗位名称") or "").strip()
        if pos and pos not in g["岗位"]:
            g["岗位"].append(pos)
        if node not in g["节点"]:
            g["节点"].append(node)
        g["hit"].append((node, node == "一面"))

    # 「这家已经不推了」的自动判据：投递表里这家**所有**记录都是「已结束」。
    # 她平时只在投递表里标一次已结束（飞书里写的「已挂」也是映射成它），
    # 进度条这边就该自己跟着沉下去、不再画后面那截灰路径 —— 不用再来这一页点第二遍。
    # ⚠️ 必须「全部记录都结束」才算：示例文具公司就是反例，一个岗位被拒、另一个岗位还在面。
    closed: Dict[str, List[bool]] = {}
    for a in apps:
        k = interview_key(str(a.get("公司名称") or "").strip())
        if not k:
            continue
        closed.setdefault(k, []).append(str(a.get("状态") or "").strip() == "已结束")

    out: List[Dict[str, Any]] = []
    for key, g in groups.items():
        manual = saved.get(key) or {}
        # 只发过「状态更新 / 资料补充」这类通知的公司不进这块 —— 没有环节可谈，
        # 硬塞进来就是一条全灰的空进度条
        if not g["节点"] and not manual:
            continue
        # 环节顺序：手改过的**以手改为准**（她删掉「AI面试」就是这家没有，别自作聪明加回来）；
        # 没手改过就用默认阶梯 + 通知里真出现过的环节（笔试 / 群面 / 三面…）。
        manual_nodes = _iv_clean_nodes(manual.get("节点"))
        if manual_nodes:
            nodes = _iv_sort_nodes(manual_nodes)
        else:
            nodes = _iv_sort_nodes(list(INTERVIEW_LADDER)
                                   + [x for x in g["节点"] if x not in INTERVIEW_LADDER])
        auto_cur, unsure = "", False
        pos_of = {n: i for i, n in enumerate(nodes)}
        for node, ns in g["hit"]:
            if pos_of.get(node, -1) > pos_of.get(auto_cur, -1):
                auto_cur, unsure = node, ns
        manual_cur = str(manual.get("当前") or "")
        cur = manual_cur or auto_cur
        # 手动写的环节里没有「通知已经推到的那一环」时补进去 —— 有邮件为证，不能让进度条空着
        if cur and cur not in pos_of:
            nodes = _iv_sort_nodes(nodes + [cur])
            pos_of = {n: i for i, n in enumerate(nodes)}
        out.append({
            "key": key,
            "公司": g["公司"] or manual.get("公司", ""),
            "岗位": g["岗位"][:3],
            "岗位数": len(g["岗位"]),
            "节点": nodes,
            "当前": cur,
            # 「知道有面试但看不出第几轮」→ 进度条上打问号，提示她点一下改成实际的轮次
            "待确认": bool(unsure and not manual_cur and cur == auto_cur),
            # 止步：手点过算止步（来源跟着记：手点=手动、邮件拒信自动标的=邮件）；
            # 没手点但这家的投递记录全结束了，也按止步算
            "止步": bool(manual.get("止步")) or (bool(closed.get(key)) and all(closed[key])),
            "止步来源": ((str(manual.get("止步来源") or "").strip() or "手动")
                        if manual.get("止步")
                        else ("已结束" if (closed.get(key) and all(closed[key])) else "")),
            # 拒信 / Offer 邮件留下的依据（主题·链接），进度行上要看得见凭据
            "邮件依据": manual.get("邮件依据") or {},
            "来源": "手动" if manual_cur else "自动",
            "通知数": g["通知数"],
            "最近": g["最近"],
            "类型": g["类型"],
        })

    out.sort(key=lambda x: x["最近"] or "", reverse=True)                    # 最近有动静的在前
    out.sort(key=lambda x: (1 if x["止步"] else 0, -_iv_rank(x["当前"])))     # 走得最远的在前，止步的垫底
    return out


def board_payload(cfg: Config) -> Dict[str, Any]:
    # 飞书 / 邮箱数据源：过期就悄悄在后台重拉一次，本次请求先返回手上的缓存
    if cfg.notices_source == "feishu" and not sync_ok_recently(cfg):
        sync_notices_bg(cfg)
    elif cfg.notices_source == "imap":
        notices_seed_state(cfg)
        if not SYNC_LOCK.locked() and not sync_ok_recently(cfg):
            sync_notices_bg(cfg)
    opps_seed_cache(cfg)   # 让「机会库」那几个数字在页面首屏就是对的
    # 投递进度：飞书表里改了状态，这里一过期就在后台拉一次，
    # 本次请求先返回手上这份（下一页轮询就是新的了）
    if cfg.apps_source == "feishu":
        apps_seed_state(cfg)
        if not apps_ok_recently(cfg):
            sync_apps_bg(cfg)
    apps = load_apps(cfg)
    notices = load_notices(cfg)
    overrides = apply_notice_states(cfg, notices)
    if cfg.notices_source == "imap":
        # 邮箱直连收来的通知，每条挂一句「凭什么这么标」（鼠标停在类型标签上能看见）
        mail_attach_evidence(cfg, notices)
    attach_notices(apps, notices)
    interview = derive_interview(cfg, notices, apps)
    if cfg.notices_source == "feishu":
        label = "飞书多维表格"
    elif cfg.notices_source == "imap":
        label = "邮箱直连（%s）" % (cfg.mail_user or cfg.mail_host)
    elif cfg.notices_csv:
        label = os.path.basename(cfg.notices_csv)
    else:
        label = "未配置"
    # 没认出公司的邮件：看板上给一条提示条，点一下归类 → 写 mail_aliases.json。
    # 只发前 30 条（就是个待办清单，不是全量列表）。
    unmatched = mail_unmatched_rows(cfg)[:30]
    return {
        "apps": apps,
        "notices": notices,
        "interview": interview,
        # 通知页「同公司归纳」的两个口径（学机会库 total / total_rows：组数和条数都报，
        # 只报一个必然有人对着表数出不一样的数）。
        # notice_companies = 同名字归组后的组数（_gkey 精确匹配，见 load_notices）；
        # notice_rows = 通知条数。都是现算的，不落盘。
        "notice_companies": len({n.get("_gkey", "") for n in notices}),
        "notice_rows": len(notices),
        # 「并入」表 + 打底表：页面拿它算归并键。两份都发下去，省得 HTML 里再抄一遍规则
        # （跟 __FIELDS__ 一个路子）。打底那份她拆掉一次就会在 app_parents 里留个空串盖住。
        "app_parents": load_app_parents(cfg),
        "app_parent_builtin": dict(APP_PARENT_BUILTIN),
        # 没认出公司的邮件（发件人 / 主题 / 摘要）—— 页面上一条提示条用它渲染，点一下归类
        "mail_unmatched": unmatched,
        "meta": {
            "version": APP_VERSION,
            "statuses": STATUSES,
            "notice_states": NOTICE_STATES,
            "notice_closed": list(NOTICE_CLOSED),
            # 进度视图那排筛选按钮要按环节先后摆，不能按字母序（"一面" 会跑到 "测评" 前面）。
            # ⚠️ 给的是 INTERVIEW_ORDER 而不是 INTERVIEW_LADDER —— 后者只有六环，
            # 笔试 / 群面 / 三面 这些会掉到 `indexOf == -1`，被排到「一面」后面去，
            # 看着就像「笔试在面试之后」。
            "iv_order": INTERVIEW_ORDER,
            "notice_overrides": overrides,
            "notices_mode": cfg.notices_source,
            "notices_source": label,
            "notices_ok": bool(cfg.notices_csv and os.path.exists(cfg.notices_csv)),
            # 邮箱直连：配没配齐、连的哪个邮箱、待确认/没认出公司各几条 —— 页面上给提示条用
            "mail_configured": bool(cfg.mail_configured),
            "mail_enabled": bool(cfg.mail_enabled),
            "mail_user": cfg.mail_user if cfg.mail_configured else "",
            "mail_host": cfg.mail_host,
            # 已连接提示条要说的「每 N 分钟自动收信」
            "mail_ttl_min": max(1, int(cfg.mail_ttl // 60)),
            "mail_unmatched_n": len(unmatched),
            # 类型拿不准、但已经作为正常一行进列表的那几条（页面给它们打「待确认」角标）。
            # 只在邮箱直连时算数：别的数据源本来就可能整列没类型，那时候这个数是噪音。
            "mail_unknown_n": (sum(1 for n in notices if not (n.get("通知类型") or []))
                               if cfg.notices_source == "imap" else 0),
            # 「待确认」角标点开能选的类型。发这份而不是让页面抄一份，免得两边漂移。
            "mail_types": list(KNOWN_MAIL_TYPES),
            "sync_msg": str(SYNC_STATE.get("msg") or ""),
            "sync_bad": SYNC_STATE.get("ok") is False,
            "sync_at": (datetime.fromtimestamp(SYNC_STATE["at"]).strftime("%H:%M:%S")
                        if SYNC_STATE.get("at") else ""),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "opps_source": cfg.opps_source,
            "opps_state": str(OPP_STATE.get("msg") or ""),
            "opps_count": int(OPP_STATE.get("count") or 0),
            # 投递进度的同步状态：页面要拿它写「每 N 分钟自动同步，上次 几点」
            "apps_source": cfg.apps_source,
            "apps_state": str(APP_STATE.get("msg") or ""),
            "apps_busy": bool(APP_STATE.get("busy")),
            "apps_bad": APP_STATE.get("ok") is False,
            "apps_sync_at": (datetime.fromtimestamp(APP_STATE["at"]).strftime("%m-%d %H:%M")
                             if APP_STATE.get("at") else ""),
            "apps_ttl_min": max(1, int(cfg.apps_ttl // 60)),
            # 距上次同步多少秒。-1 = 这个进程还没同步过。页面拿它决定「要不要顺手拉一下」
            # （TTL 30 分钟，但你刚投完切过来看时等不起 —— 见 nudgeApps）。
            "apps_age_s": (int(max(0, time.time() - float(APP_STATE["at"])))
                           if APP_STATE.get("at") else -1),
            # 上一次同步实际记了什么，页面用来在标题栏报一句「刚进来 1 条：××」
            "apps_changes": [{"公司名称": str(c.get("公司名称") or ""),
                              "岗位名称": str(c.get("岗位名称") or ""),
                              "diff": [str(x) for x in (c.get("diff") or [])]}
                             for c in _APP_LAST_CHANGES[:5]],
        },
    }


# ---------------------------------------------------------------------------
# 看板页面
# ---------------------------------------------------------------------------

# 深色模式的 token，单独一份 —— 页面里注入两次（系统偏好 / 手动切深色）。
# 只存一份是硬要求：以前直接写在 @media 里，要加「手动切深色」就得抄第二遍，
# 两份迟早会走样（改了浅色忘了深色，深色下某个块就变成刺眼的白）。
DARK_TOKENS = r"""--bg:#0e1013;--card:#181a1e;--line:#282b31;--line2:#202327;--hover:#212429;
--fg:#e9ebef;--muted:#98a0ac;--faint:#6b717b;--track:#25282e;
--red:#e4685f;--orange:#d9a33c;--green:#4fbb8e;--blue:#6fa3f0;--purple:#a98cf0;
--gray:#6b717b;--accent:#7f95f7;--accent-soft:#1e2334;
--glow:rgba(127,149,247,.05);
--t-total:rgba(127,149,247,.09);--t-blue:rgba(111,163,240,.09);--t-purple:rgba(169,140,240,.09);
--t-green:rgba(79,187,142,.09);--t-red:rgba(228,104,95,.09);--t-orange:rgba(217,163,60,.09);
--t-gray:rgba(152,160,172,.07);
--shadow:0 1px 2px rgba(0,0,0,.36),0 1px 3px rgba(0,0,0,.3);
--shadow-h:0 6px 18px rgba(0,0,0,.42);
--ring:0 0 0 3px rgba(127,149,247,.26);
--on-accent:#0e1013;--on-red:#260d0b;color-scheme:dark"""


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>秋招看板</title>
<script>
/* 主题必须在样式表之前定下来 —— 这段跑在 <head> 里，晚一步（比如放到 body 末尾）
   刷新时就会先闪一屏浅色再跳到深色。
   只写 data-theme-mode（auto / light / dark）：选「自动」时**不写** data-theme，
   交给 CSS 的 prefers-color-scheme 去跟 —— 这样系统夜里自动转深色时页面会跟着变。
   这段不能用下面脚本里的 lsLoad，所以自己小心地读一次；读不动就当自动。 */
(function(){
  var m = "auto";
  try{
    var v = JSON.parse(localStorage.getItem("at.theme.v1") || "null");
    if(v && (v.mode === "light" || v.mode === "dark")) m = v.mode;
  }catch(e){}
  var r = document.documentElement;
  r.setAttribute("data-theme-mode", m);
  if(m !== "auto") r.setAttribute("data-theme", m);
  /* 邮箱页那两个视图（面试测评截止 / 投递进度）也在这儿定：晚一步就会先闪一下
     默认视图再跳过去。上个会话停在哪一版，这一屏就直接开哪一版。 */
  var nv = "dl";
  try{
    if(JSON.parse(localStorage.getItem("at.ntv.v1") || "null") === "iv") nv = "iv";
  }catch(e){}
  r.setAttribute("data-ntv", nv);
})();
</script>
<style>
:root{
/* 中性色阶：全套冷灰一个色系（别混暖灰）。文字三级 —— fg 正文 / muted 次要 / faint 弱化 */
--bg:#f6f7f9;--card:#fff;--line:#e4e7ec;--line2:#f0f2f5;--hover:#f8fafc;
--fg:#101828;--muted:#5d6470;--faint:#98a0ac;--track:#eaecf0;
/* 语义色：降饱和，跟中性灰放一起不打架 */
--red:#c8382f;--orange:#a9670f;--green:#0f7a55;--blue:#2b6cb8;--purple:#6f46c4;
--gray:#98a0ac;--accent:#3b5bdb;--accent-soft:#eef1fd;
--glow:rgba(59,91,219,.04);
--t-total:rgba(59,91,219,.05);--t-blue:rgba(43,108,184,.05);--t-purple:rgba(111,70,196,.05);
--t-green:rgba(15,122,85,.055);--t-red:rgba(200,56,47,.05);--t-orange:rgba(169,103,15,.07);
--t-gray:rgba(152,160,172,.10);
/* 阴影：一层轻投影撑住层级就够。「三层大阴影」是 AI 生成页面的典型特征，产品界面不靠它 */
--shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.04);
--shadow-h:0 4px 12px rgba(16,24,40,.08),0 2px 4px rgba(16,24,40,.05);
--ring:0 0 0 3px rgba(59,91,219,.18);
/* 圆角三档，全站就这三条：卡片 14 / 控件 9 / 胶囊 999。别再冒出 10px、16px 这种散值 */
--r:14px;--r-sm:9px;--r-pill:999px;
/* 铺在强调色上的字色做成 token：浅色下是白字，深色下强调色本身是浅蓝、必须配深字
   （WCAG AA）。原来靠文件末尾一条 @media 单独盖 button.primary —— 一旦改成手动切换，
   那条就漏了，所以收进 token。 */
--on-accent:#fff;
/* 铺在 --red 上的字色同理：浅色下红是深红、配白字；深色下红是浅红、得配深字。
   红底最常出现在进度条末尾那个「已挂」标上（v0.24.1）。 */
--on-red:#fff;
/* 让原生控件（下拉箭头、滚动条、搜索框的清除钮）跟着主题走，不然深色下会冒出白块 */
color-scheme:light}
/* ---- 深色 token 只有一份（Python 里的 DARK_TOKENS，这里注入两处）----
   ① 系统偏好：只有「没手动指定」时才跟，指定了就听人的；
   ② 手动切深色。
   两处缺一不可：少了 ① 就丢掉「自动跟随」，少了 ② 手动切不动。 */
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){__DARK__}}
:root[data-theme=dark]{__DARK__}
*{box-sizing:border-box}
body{margin:0;padding:30px 22px 60px;background:var(--bg);color:var(--fg);
background-image:radial-gradient(1100px 480px at 50% -240px,var(--glow),transparent);
background-repeat:no-repeat;
font:14px/1.62 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
/* 全站数字等宽：表格里的日期、条数、天数上下对齐，扫一眼就能比大小 */
font-variant-numeric:tabular-nums;
-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
/* 顶边一条 2px 强调色细线。原来那条「透明→蓝→紫→透明」的渐变是典型的 AI 味装饰，
   而且违反「全站一个强调色」—— 换成单色。 */
body::before{content:"";position:fixed;left:0;right:0;top:0;height:2px;z-index:10;
background:var(--accent);opacity:.9}
.wrap{max-width:1280px;margin:0 auto}
.top{display:flex;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:23px;font-weight:700;margin:0;letter-spacing:.2px}
h1 .ver{display:inline-block;margin-left:9px;padding:2px 9px;border-radius:999px;
background:var(--accent-soft);color:var(--accent);font-size:11px;font-weight:650;
letter-spacing:.4px;vertical-align:4px;font-variant-numeric:tabular-nums}
.brand{color:var(--muted);font-size:12.5px;margin-top:5px}
.sub{color:var(--muted);font-size:12.5px;margin-left:auto;text-align:right;line-height:1.9}
.tabs{display:flex;gap:6px;margin-bottom:16px;border-bottom:1px solid var(--line)}
.tab{padding:10px 16px;border:none;background:transparent;color:var(--muted);font-size:14px;
font-family:inherit;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;
border-radius:10px 10px 0 0;transition:color .12s ease,background .12s ease}
.tab:hover{color:var(--fg);background:var(--line2)}
.tab.on{background:transparent;color:var(--fg);border-bottom-color:var(--accent);font-weight:650}
.tab .n{display:inline-block;margin-left:7px;padding:0 7px;border-radius:999px;background:var(--track);
color:var(--muted);font-size:11.5px;line-height:1.75;font-weight:500;vertical-align:1px;
font-variant-numeric:tabular-nums;transition:background .12s ease,color .12s ease}
.tab.on .n{background:var(--accent-soft);color:var(--accent)}
.pane{display:none}
.pane.on{display:block}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin-bottom:14px}
.kpi-card{position:relative;text-align:left;background:var(--card);border:1px solid var(--line);
border-radius:var(--r);padding:18px 16px 15px;cursor:pointer;color:var(--fg);font:inherit;
box-shadow:var(--shadow);overflow:hidden;
/* 默认纯白卡面。语义色只在鼠标移上来时透出极淡一层 —— 十几张卡各带一块彩色渐变的底，
   是「AI 生成感」最重的地方之一，产品界面不该那样铺色。 */
transition:transform .14s ease,border-color .14s ease,box-shadow .14s ease}
.kpi-card:hover{transform:translateY(-2px);box-shadow:var(--shadow-h);
background-image:linear-gradient(180deg,var(--tint,transparent) 0,transparent 46px)}
.kpi-card:active{transform:translateY(0)}
.kpi-card b{display:block;font-size:33px;line-height:1.08;font-weight:700;letter-spacing:-.4px;
font-variant-numeric:tabular-nums}
.kpi-card span{display:block;color:var(--muted);font-size:12.5px;margin-top:6px;white-space:nowrap;letter-spacing:.2px}
.kpi-card::before{content:"";position:absolute;left:0;right:0;top:0;height:3px;background:var(--faint);opacity:.5}
.kpi-card.t-total{--tint:var(--t-total)}
.kpi-card.t-active{--tint:var(--t-blue)}
.kpi-card.t-interview{--tint:var(--t-purple)}
.kpi-card.t-offer{--tint:var(--t-green)}
.kpi-card.t-closed{--tint:var(--t-gray)}
.kpi-card.t-today{--tint:var(--t-red)}
.kpi-card.t-soon{--tint:var(--t-orange)}
.kpi-card.t-over{--tint:var(--t-gray)}
.kpi-card.t-done{--tint:var(--t-green)}
.kpi-card.t-late{--tint:var(--t-red)}
.kpi-card.t-total::before{background:var(--accent);opacity:1}
.kpi-card.t-active::before{background:var(--blue);opacity:1}
.kpi-card.t-interview::before{background:var(--purple);opacity:1}
.kpi-card.t-offer::before{background:var(--green);opacity:1}
.kpi-card.t-closed::before{background:var(--gray);opacity:1}
.kpi-card.t-today::before{background:var(--red);opacity:1}
.kpi-card.t-soon::before{background:var(--orange);opacity:1}
.kpi-card.t-over::before{background:var(--gray);opacity:1}
.kpi-card.t-done::before{background:var(--green);opacity:1}
.kpi-card.t-late::before{background:var(--red);opacity:1}
.kpi-card.t-late b{color:var(--red)}
.kpi-card.on{--tint:var(--accent-soft);background-color:var(--card);color:var(--fg);
border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),var(--shadow-h)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-bottom:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:17px 18px 16px;box-shadow:var(--shadow)}
.panel h2{font-size:12.5px;font-weight:650;color:var(--fg);opacity:.78;margin:0 0 14px;letter-spacing:.3px}
.brow{display:grid;grid-template-columns:112px 1fr 36px;align-items:center;gap:11px;margin-bottom:11px}
.brow:last-child{margin-bottom:0}
.brow .bl{font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brow .bt{height:9px;border-radius:999px;background:var(--track);overflow:hidden}
.brow .bt i{display:block;height:100%;border-radius:999px;background:var(--faint);transition:width .34s ease}
.brow .bn{font-size:12.5px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.brow.zero .bl,.brow.zero .bn{color:var(--faint);font-weight:400}
.prog-wrap{display:flex;align-items:baseline;gap:10px;margin-bottom:11px}
.prog-num{font-size:30px;font-weight:700;letter-spacing:-.5px;font-variant-numeric:tabular-nums;line-height:1.08}
.prog-track{height:10px;border-radius:999px;background:var(--track);overflow:hidden;margin-bottom:15px}
.prog-track i{display:block;height:100%;background:var(--green);border-radius:999px;transition:width .34s ease}
.kv{display:flex;justify-content:space-between;gap:12px;font-size:12.5px;padding:8px 0;
border-top:1px solid var(--line2)}
.kv:first-of-type{border-top:none;padding-top:0}
.kv span{color:var(--muted)}
.kv b{font-weight:650;font-variant-numeric:tabular-nums;white-space:nowrap}
/* 近 7 天投递趋势：竖条，今天在最右边一根。
   跟「状态分布 / 投递节奏」那两条横条不同 —— 横条比的是「各类占多少」，
   这里比的是「一天比一天多还是少」，竖着才看得出来。
   ⚠️ 柱子高度**不写死**（原来是 62px）：她 2026-10-10 要「趋势图大一点」，而这块面板
   底下被砍到只剩两行，写死高度会在面板里留一大条空白。改成柱子撑满面板剩下的高度 ——
   `.panel` 是 grid 项，跟旁边两块等高，多出来的高度整块给图。
   ⚠️ 所以 `.tchart` 必须 `align-items:stretch`（不是 flex-end）：要让 `.tcol` 拉满高，
   `.tt` 的 `flex:1` 才撑得起来，里面 `i` 的 `height:％` 才有高度可算。
   ⚠️ `min-height` 才是「多大」的旋钮（`flex:1` 只在旁边两块更高时才有富余可分）：
   原来写死 62px，砍完内容后旁边两块只有 249px，柱子反而缩到 57px —— 比改之前还小。
   所以这儿得**自己把面板顶高**（160 + 上下固定 41 ≈ 轨道 119px，是原来的两倍），
   旁边两块跟着等高，跟 2026-10-09 那次一样。 */
.panel.trend{display:flex;flex-direction:column}
.panel.trend #appTrend{flex:1;display:flex;flex-direction:column;min-height:0}
.tchart{flex:1;display:flex;gap:7px;align-items:stretch;min-height:160px}
.tcol{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:7px}
.tcol .tnum{font-size:14px;font-weight:650;font-variant-numeric:tabular-nums;color:var(--faint);line-height:1}
.tcol.has .tnum{color:var(--fg)}
.tcol .tt{width:100%;flex:1;min-height:0;display:flex;align-items:flex-end;background:var(--track);
border-radius:6px;overflow:hidden}
.tcol .tt i{display:block;width:100%;height:0;border-radius:6px 6px 0 0;background:var(--faint);transition:height .34s ease}
.tcol.has .tt i{background:var(--accent)}
.tcol.today .tt i{background:var(--orange)}
.tcol .tl{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.tcol.today .tl{color:var(--fg);font-weight:650}
/* 图底下只剩两行数（她 2026-10-10：「趋势图大一点，不要下面的文字，
   可以留最近一次投递和近 7 天投递数量」）。
   ⚠️ **别的数一个都没扔** —— 全挂成这一块的 `title`，鼠标停上去照旧看得见；
   原来那句「另有 N 家不在这一周内」也在那儿。所以别再往这儿加第五行第六行。
   ⚠️ 这儿**不能**用两列网格：一格只有 ~155px，中文标签会被挤成一列一个字。 */
.ov{flex:none;margin-top:11px;padding-top:10px;border-top:1px solid var(--line2)}
.ov .kv{padding:3px 0;font-size:12.5px}
.ov .kv span{white-space:nowrap}
.muted-line{color:var(--muted);font-size:12.5px}
.bar{display:flex;gap:9px;flex-wrap:wrap;align-items:center;background:var(--card);border:1px solid var(--line);
border-radius:var(--r);padding:11px 14px;box-shadow:var(--shadow);margin-bottom:12px}
input[type=search],input[type=text],input[type=url],select{padding:8px 13px;border:1px solid var(--line);
border-radius:var(--r-sm);background:var(--card);color:var(--fg);font-size:13px;font-family:inherit;
transition:border-color .12s ease,box-shadow .12s ease}
input[type=search]{min-width:200px}
input[type=search]:focus,input[type=text]:focus,input[type=url]:focus{
outline:none;border-color:var(--accent);box-shadow:var(--ring)}
/* 控件圆角 9px（跟卡片 14 / 胶囊 999 是一套三档规则）。原来全是 999px 胶囊，
   一排筛选片看着像药丸墙；产品界面用方圆角更稳，状态胶囊才保留 999。 */
button{padding:6px 13px;border:1px solid var(--line);border-radius:var(--r-sm);background:var(--card);
color:var(--muted);font-size:12.5px;font-family:inherit;cursor:pointer;
transition:border-color .12s ease,background .12s ease,color .12s ease,transform .08s ease}
button:hover{border-color:var(--faint);color:var(--fg);background:var(--hover)}
/* 按下去要有被按到的感觉。排除 .gtoggle：它是公司名，抖一下会显得整行在闪 */
button:not(.gtoggle):active{transform:translateY(1px)}
button.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
button.on:hover{color:var(--on-accent);border-color:var(--accent)}
/* 主按钮做成实心：描边版在一排灰按钮里根本看不出哪个是「主」动作 */
button.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
button.primary:hover{background:var(--accent);border-color:var(--accent);color:var(--on-accent);
filter:brightness(.94)}
/* ---- 配色切换（自动 / 浅色 / 深色）----
   必须放在上面那几条通用 button 规则**之后**：`.themebar button` 与 `button:hover`
   特异度相同（都是 0,1,1），同分时靠源码顺序定胜负 —— 放前面会被 hover 的灰底盖掉。 */
.themebar{margin-left:auto;align-self:center;margin-bottom:2px;display:flex;gap:2px;padding:2px;
background:var(--line2);border:1px solid var(--line);border-radius:var(--r-pill)}
.themebar button{border:none;background:transparent;color:var(--muted);font:inherit;font-size:12px;
line-height:1.5;padding:3px 11px;border-radius:var(--r-pill)}
.themebar button:hover{background:transparent;color:var(--fg)}
.themebar button.on{background:var(--card);color:var(--fg);font-weight:650;box-shadow:var(--shadow)}
/* ---- 邮箱页：两块都在一页上（面试进度在上，截止通知在下）----
   不再互斥切换了 —— 她的原话是「要保留之前的时间截止通知，在进度条后面加一个」。
   两块都是普通流里的内容，谁也不靠 display:none 藏着（藏了就又变成「功能没了」）。 */
.secttl{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin:6px 0 12px}
.secttl b{font-size:12.5px;font-weight:650;color:var(--fg);opacity:.78;letter-spacing:.3px}
.secttl em{font-style:normal;font-size:12.5px;color:var(--muted);min-width:0}
.sep{width:1px;height:20px;background:var(--line)}
.grow{flex:1}
#count{font-size:12.5px;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.form{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:15px 17px;
box-shadow:var(--shadow);margin-bottom:12px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.form input{flex:1;min-width:150px}
/* display:flex 会把 hidden 属性盖掉，「+ 手动加一条」那个开关就失灵了（表单一直摊着） */
.form[hidden]{display:none}
.tbl-wrap{background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:auto;
box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse;min-width:940px}
th,td{padding:13px 14px;text-align:left;font-size:13.5px;vertical-align:top}
th{position:sticky;top:0;background:var(--line2);color:var(--fg);font-weight:650;font-size:11.5px;
letter-spacing:.4px;white-space:nowrap;border-bottom:1px solid var(--line);z-index:1}
td{border-bottom:1px solid var(--line2)}
/* 悬停用强调色浅底（Linear / Notion 那一套）。原来只是比背景深一点的灰，
   一整屏白底表格里几乎看不出鼠标停在哪一行。 */
tbody tr:hover td{background:var(--accent-soft)}
tbody tr:last-child td{border-bottom:none}
tbody tr.closed td{opacity:.6}
.c-name{font-weight:700;white-space:nowrap;font-size:14px;letter-spacing:.1px}
.c-pos{color:var(--muted)}
.c-topic{color:var(--muted);font-size:12.5px;max-width:400px}
.dl small{display:block;color:var(--faint);font-size:11.5px;margin-top:2px}
/* ---- 同公司多岗位 / 归并组：主行 + 子行 ---- */
/* 主行：整条淡底、上边线，跟子行拉开层级；子行左侧一道竖线＋圆点，一眼看出从属关系。
   两层都走这套：顶层是「公司 / 主公司」，第二层是归并组里的子公司（tr.grp.lvl2）。
   计数口径也跟着变 —— KPI「总投递」按**行**数算（主公司算一行），
   源表的原始家数在「概览」里另报一格（见 renderApps / buildNodes）。 */
tr.grp td{background:var(--line2);border-bottom:1px solid var(--line);
vertical-align:middle;padding-top:10px;padding-bottom:10px}
/* 整行可点（不用非得去点那个 10px 的小三角），所以整行给手型；鼠标划过时
   箭头和公司名一起变强调色 —— 不这样的话看不出「这一行能点」。
   特异性 (0,3,2) 压过上面的 .gtoggle 默认色 (0,2,1)，别看代码顺序。 */
tr.grp{cursor:pointer}
tbody tr.grp:hover td{background:var(--accent-soft)}
tbody tr.grp:hover .gtoggle,
tbody tr.grp:hover .gtoggle .arw{color:var(--accent)}
tr.grp .gtoggle{display:inline-flex;align-items:center;gap:7px;border:none;background:transparent;
padding:0;font:inherit;color:var(--fg);font-weight:700;font-size:14px;letter-spacing:.1px;
cursor:pointer;text-align:left}
tr.grp .gtoggle:hover{background:transparent;border:none;color:var(--accent)}
tr.grp .gtoggle .arw{color:var(--faint);font-size:10px;transition:transform .14s ease;flex:none}
tr.grp.open .gtoggle .arw{transform:rotate(90deg)}
/* 主行第二行：岗位数徽标 + 岗位名列表。**宽度必须卡在公司名那一列里**（限宽 < 公司列宽），
   否则主行会把整张表撑宽 —— 她屏幕 1080px，表格本来刚好铺满，多一个像素就要左右拖。 */
tr.grp .gmeta{display:flex;align-items:center;gap:6px;max-width:300px;margin-top:4px}
tr.grp .gn{flex:none;padding:1px 8px;border-radius:999px;background:var(--card);
border:1px solid var(--line);color:var(--muted);font-size:11px;font-weight:600;white-space:nowrap}
tr.grp .gpos{flex:1 1 auto;min-width:0;color:var(--muted);font-size:12px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
tr.sub td:first-child{position:relative;padding-left:34px}
tr.sub td:first-child::before{content:"";position:absolute;left:17px;top:0;bottom:0;
border-left:2px solid var(--line)}
tr.sub .subdot{position:absolute;left:14px;top:50%;width:9px;height:9px;margin-top:-4.5px;
border-radius:50%;background:var(--card);border:2px solid var(--line)}
tr.sub:hover .subdot{border-color:var(--accent)}
tr.sub td.c-pos{color:var(--fg);font-weight:600}
/* 「并入 / 拆出」：**竖着堆在 × 上面**，不是并排。这张表在 1080px 下已经顶到容器边
   （实测列宽合计 1050 = 容器 1050，横向一个像素的余量都没有），并排加两个字必然
   撑出横向滚动条；而操作那格本来就只放着一个 27px 的 ×、行高 71px，上面还空着大半格。
   字号和左右内边距都收着给，让它比 × 窄一点（23px < 27px），列宽就一个像素都不动。 */
td.c-op{white-space:nowrap}
button.mg{display:block;border:none;background:transparent;color:var(--faint);cursor:pointer;
font-family:inherit;font-size:11.5px;line-height:1.55;padding:1px 0;margin:0 auto}
button.mg:hover{color:var(--accent);background:var(--line2)}
button.mg.on{color:var(--muted)}
/* 拖动并入（v0.27.0）：把「代表一家公司」的那一行**拖到另一家那一行上**，松手就并进去。
   点按钮要开面板、还得把主公司名打一遍（「示例建筑集团」这种表里本来没有的名字只能手打），
   十来家子公司就是十几次手打 —— 拖动只动鼠标。
   落点整行描一圈强调色。开头那两个 `!important` 是**必须的**：`tr.grp td`（淡底）
   和 `tbody tr:hover td`（划过变色）两条规则的背景都压过这里，不写就什么都看不出来。
   光标：能拖的行是 `grab`（张手），拖起来是 `grabbing`（抓拳）—— 顺带也还是手型，
   「这一行能点」这条没丢。 */
tr[draggable="true"]{cursor:grab}
tr[draggable="true"]:active{cursor:grabbing}
tr.dragging{opacity:.45}
tr.dropok>td{background:var(--accent-soft)!important;box-shadow:inset 0 0 0 2px var(--accent)}
/* 归并组里那家子公司：比顶层缩进一格、字重轻一档，一眼看出是「挂在上面那家下面」 */
tr.grp.lvl2 .gtoggle{padding-left:12px;font-weight:600;font-size:13px}
tr.grp.lvl2 .gtoggle .arw{color:var(--line);font-size:9px}
/* 归并组的子行：第一列要写清是哪家子公司。跟 .gpos 一样限宽 + 省略号 ——
   公司名长的（「示例国际城市开发有限公司」）不许把公司那一列撑宽。 */
tr.sub .subco{display:inline-block;vertical-align:middle;max-width:190px;margin-left:6px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);
font-size:12px;font-weight:600}
/* 「展开全部 / 收起全部」按钮只在真有公司折叠时才出现 */
#appOpenAll[hidden]{display:none}
/* 备注点一下就地编辑（改完立刻落盘，跟「状态」一个路子）。
   2026-10-08 无障碍 A1：跟 .cellv 同一批，<span> → 原生 <button>（键盘够得着、读屏认得出）。
   UA 默认样式同样在这里抹掉，观感不变。 */
.noteview{display:inline-block;min-width:38px;padding:3px 7px;margin:-3px -7px;border-radius:var(--r-sm);
border:none;background:none;color:inherit;font-family:inherit;font-size:inherit;text-align:left;
cursor:text;white-space:pre-wrap;word-break:break-word;transition:background .12s ease,color .12s ease}
.noteview:hover{background:var(--accent-soft);color:var(--accent)}
/* 备注列在 1080px 下只有 ~49px 宽，「＋ 加备注」会被挤成两行（加难看的孤字行）。
   文案缩成 3 字 + 禁止折行就放得下了，列宽不受影响（实测三档总宽不变）。 */
.noteadd{color:var(--faint);font-size:12px;white-space:nowrap}
.noteview:hover .noteadd{color:var(--accent)}
/* 备注的编辑框不再单独写一套样式（原来是 `width:100%;min-width:130px`，而备注列只有 72px：
   一点编辑，整张表被撑到 1131px（总宽 +81）、横向溢出，22 行的文字跟着重新换行）。
   v0.23.1 起备注也走 `td input.cellin` 那套**浮框**，`editNote()` 直接委托 `inlineEdit()`。 */
/* 整行都能改（v0.23.0）：公司名 / 岗位名 / 投递时间 跟备注一个观感 ——
   平时就是普通文字，鼠标移上去才浮出浅底，不点不乱。 */
/* 2026-10-08 无障碍 A1：.cellv 从 <span> 改成了原生 <button>（可点操作就该是按钮，
   键盘 Tab 得到、读屏认得出）。<button> 自带一坨 UA 默认样式（底色 / 边框 / 字体 / 居中），
   全部在这里抹掉 —— 抹完观感跟原来的 <span> 一模一样，只是多了键盘可达。
   `overflow-wrap` 而不是 `word-break:break-word` —— 后者的规范等价物是 `overflow-wrap:anywhere`，
   它会把这格的**最小内容宽度**压成 1 个字，auto 表格布局就能把「岗位」列从 114px 挤到 84px
   （空出来的 26px 分给了「备注」），岗位名当场变成一字一行。`break-word` 只断超长词、
   不动内在尺寸 —— 换完列宽逐列对上了 v0.22.0（实测 [364,114,112,149,116,75,72,49]）。 */
.cellv{display:inline-block;max-width:100%;padding:3px 7px;margin:-3px -7px;border-radius:var(--r-sm);
border:none;background:none;color:inherit;font-family:inherit;font-size:inherit;text-align:left;
cursor:text;overflow-wrap:break-word;transition:background .12s ease,color .12s ease,box-shadow .12s ease}
/* 悬停必须是「一张描了边的白底小片」，不能只是一层浅底 ——
   `tbody tr:hover td` 本来就是 accent-soft，用同一层色等于没有反馈（实测截图里几乎看不出）。
   备注那格一起改成同一套，整行的「能点」提示才一致。 */
.cellv:hover,.noteview:hover{background:var(--card);color:var(--accent);
box-shadow:inset 0 0 0 1px var(--accent)}
.celladd{color:var(--faint);font-size:12px;white-space:nowrap}
.cellv:hover .celladd,.noteview:hover .noteadd{color:var(--accent)}
/* 就地编辑的浮层**整个浮在格子上方**，一个像素都不动表格布局 ——
   原来的内容用 visibility:hidden 继续占位（占位 = 尺寸不变），浮层绝对定位盖上去。
   早先那版是 width:100% 填满格子，可百分比宽度会被 auto 表格布局当成内在尺寸算：
   链接列能从 76px 撑到 119px，连带 134 行文字重新换行，看着像把表弄坏了。
   v0.24.2：浮层里除输入框还能放一排「今天 / 昨天 / 前天」快捷按钮，所以绝对定位从 input
   挪到外层 `.cellpop` 上（input 改静态、交给 flex 排）—— 定位只有一层要管，`anchor-r` 也只一处。
   选择器带 td 是为了压过通用那条 input[type=text]（同分时才轮到源码顺序，别赌它）。 */
td.editing{position:relative}
td.editing > *:not(.cellpop){visibility:hidden}
td .cellpop{position:absolute;top:50%;left:0;transform:translateY(-50%);z-index:3;
display:flex;align-items:center;gap:6px}
/* 靠右半边的格子贴右边缘往左铺，否则浮层会顶出表格右边 */
td.editing.anchor-r .cellpop{left:auto;right:0}
td input.cellin{width:240px;padding:5px 9px;border:1px solid var(--accent);
border-radius:var(--r-sm);background:var(--card);color:var(--fg);font-size:12.5px;font-family:inherit;
box-shadow:0 8px 20px rgba(0,0,0,.18)}
/* 时间那格旁边还排着三个快捷按钮，输入框收窄一点，整条浮层才不至于太长 */
td .cellpop.has-quick input.cellin{width:186px}
td input.cellin:focus{outline:none;box-shadow:0 0 0 3px var(--accent-soft),0 8px 20px rgba(0,0,0,.18)}
/* 「今天 / 昨天 / 前天」：投递时间那格是空的（「＋时间」）时，原来只能从零手打一个日期 ——
   她 2026-10-05 的原话：「改时间方式能不能方便一点，还要我一个个手打。。」
   现在点一下直接存（不用点日历、也不用敲键盘），观感跟表里其它小控件一致。 */
.cellquick{display:flex;align-items:center;gap:4px;white-space:nowrap}
.cellquick button{padding:4px 8px;font-size:12px;font-family:inherit;line-height:1.5;cursor:pointer;
border:1px solid var(--line);border-radius:var(--r-sm);background:var(--card);color:var(--muted)}
.cellquick button:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
/* 链接列只有 72px，左右内边距就吃掉 28px ——「打开」胶囊（46px）一摆就满了，
   编辑入口根本挤不进同一行（硬排的结果：127 行的 ✎ 掉到第二行，行高当场 +8px，
   或者绝对定位压到胶囊上，看着像画坏了）。
   所以做成**第二行的淡淡文字**，跟「＋备注」「＋岗位」同一个样子：
   有链接写「改链接」、没链接写「＋链接」，一行字 18px 高，代价只有平均 +3px 行高。 */
button.lnkedit{padding:0;border:none;background:none;color:var(--faint);font-family:inherit;
font-size:12px;line-height:1.5;text-align:left;white-space:nowrap;cursor:pointer}
button.lnkedit:hover{color:var(--accent)}
td.haslink button.lnkedit{display:block;width:100%;margin-top:1px}
td.pill{white-space:nowrap}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;line-height:1.7;
border:none;background:var(--track);color:var(--muted);margin:0 4px 3px 0;cursor:pointer;
font-family:inherit;font-weight:600;transition:opacity .12s ease}
.tag:hover{opacity:.72}
select.st,select.sts{padding:4px 9px;border-radius:999px;font-size:12px;background:var(--card);
border:1px solid var(--line);color:var(--fg);font-family:inherit;cursor:pointer;
transition:border-color .12s ease,background .12s ease}
.pillst{display:inline-block;padding:2.5px 10px;border-radius:999px;font-size:12px;
font-weight:650;white-space:nowrap;line-height:1.75}
.st-已投递,.st-简历通过{color:var(--blue);background:var(--t-blue)}
.st-测评笔试{color:var(--orange);background:var(--t-orange)}
.st-面试{color:var(--purple);background:var(--t-purple)}
.st-Offer{color:var(--green);background:var(--t-green)}
.st-已结束{color:var(--faint);background:var(--line2)}
select.st.st-已投递,select.st.st-简历通过{color:var(--blue);background:var(--t-blue);border-color:var(--blue)}
select.st.st-测评笔试{color:var(--orange);background:var(--t-orange);border-color:var(--orange)}
select.st.st-面试{color:var(--purple);background:var(--t-purple);border-color:var(--purple)}
select.st.st-Offer{color:var(--green);background:var(--t-green);border-color:var(--green)}
select.st.st-已结束{color:var(--faint);background:var(--line2);border-color:var(--line)}
.dl{font-variant-numeric:tabular-nums;white-space:nowrap}
.dl.over{color:var(--gray)}
/* ---- 邮箱通知页 ---- */
.kpi-card.t-today b{color:var(--red)}
.kpi-card.t-over b{color:var(--muted)}
.kpi-card.t-done b{color:var(--green)}
.kpi-card em{display:block;font-style:normal;color:var(--faint);font-size:11.5px;margin-top:3px}
.alert{display:flex;align-items:center;gap:9px;flex-wrap:wrap;background:var(--card);
border:1px solid var(--line);border-left:3px solid var(--green);border-radius:var(--r);
padding:12px 16px;margin-bottom:12px;box-shadow:var(--shadow);font-size:13px}
.alert.urgent{border-left-color:var(--red)}
.alert .al{font-weight:700;white-space:nowrap}
.alert .ai{color:var(--muted);font-size:12.5px}
.alert .it{white-space:nowrap;font-variant-numeric:tabular-nums;background:var(--line2);
padding:2px 10px;border-radius:999px;font-size:12px;color:var(--muted)}
.alert .it b{font-weight:650;color:var(--fg)}
.alert .it.hot b{color:var(--red)}
.alert .it.warm b{color:var(--orange)}
tbody tr.g-over td:first-child,tbody tr.g-today td:first-child{box-shadow:inset 3px 0 0 var(--red)}
tbody tr.g-d3 td:first-child{box-shadow:inset 3px 0 0 var(--orange)}
tbody tr.g-d7 td:first-child{box-shadow:inset 3px 0 0 var(--blue)}
tbody tr.sect td{background:var(--line2);
color:var(--fg);font-size:12.5px;font-weight:700;letter-spacing:.3px;white-space:nowrap;
padding:10px 14px;border-bottom:1px solid var(--line);border-top:1px solid var(--line)}
/* ⚠️ 与 .ntcor 同一条铁律（2026-10-09 顺手量出来的老坑）：flex 绝不能挂在 <td colspan="9"> 上，
   那会让格子**退出表格布局**、colspan 当场失效 —— 分段行实测宽度 219px / 表宽 1050px，
   「今天截止」「3 天内截止」这些标题被压成窄窄一条。flex 落在这层内层 div 上。 */
tbody tr.sect td>.sectr{display:flex;align-items:center;gap:9px}
tbody tr.sect.g-over td,tbody tr.sect.g-today td{box-shadow:inset 3px 0 0 var(--red)}
tbody tr.sect.g-d3 td{box-shadow:inset 3px 0 0 var(--orange)}
tbody tr.sect.g-d7 td{box-shadow:inset 3px 0 0 var(--blue)}
tbody tr.sect.g-later td{box-shadow:inset 3px 0 0 var(--muted)}
tbody tr.sect.g-unknown td{box-shadow:inset 3px 0 0 var(--faint)}
/* 「标了已完成、但截止还没到」（2026-10-09 新增的那一段）：橙色，跟「3 天内截止」一个色系 —— 它在
   时间上还是一件没结束的事，不该跟灰区一个待遇。 */
tbody tr.sect.g-check td{box-shadow:inset 3px 0 0 var(--orange)}
tbody tr.sect.g-done td{box-shadow:inset 3px 0 0 var(--green)}
tbody tr.sect .sn{color:var(--muted);font-weight:500;letter-spacing:0;margin-left:auto;
font-variant-numeric:tabular-nums}
tbody tr.sect .dot{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:none}
/* 「按公司归纳」的公司行（她 2026-10-08 拍板的形态）。自己就是一套完整的「单格 flex 组头」，
   不借 .sect 的类 —— 那是紧急度分段行（只在平铺形态出现）的，两种行的口径要分得开。
   全部已完成的公司行整行灰显（跟明细行的 done 一个待遇）。 */
tbody tr.ntco{cursor:pointer}
tbody tr.ntco td{background:var(--card);
  box-shadow:inset 3px 0 0 var(--blue);cursor:pointer;font-weight:600}
/* ⚠️ flex 必须挂在这层**内层 div**上，绝不能直接挂在 <td colspan="9"> 上（2026-10-09 实测踩到）：
   `display:flex` 会让这个格子**退出表格布局**，colspan 当场失效 —— 宽度塌成第一列那点
   （实测 219px / 表宽 1050px），里面每个子元素都被迫缩到极限，四五个字的公司名被挤成
   一个字一行堆起来的竖条。表格是 table-layout:fixed，格子必须保持 display:table-cell。 */
tbody tr.ntco td>.ntcor{display:flex;align-items:center;gap:9px}
tbody tr.ntco:hover td{background:var(--line2)}
tbody tr.ntco.done td{color:var(--faint);box-shadow:inset 3px 0 0 var(--faint)}
tbody tr.ntco .ntexp{border:none;background:none;color:inherit;font:inherit;font-weight:600;
  cursor:pointer;padding:0;display:inline-flex;align-items:center;gap:5px;text-align:left}
tbody tr.ntco .ntexp .arw{display:inline-block;font-size:10px;color:var(--faint);flex:none;
  transition:transform .14s ease}
tbody tr.ntco .ntexp[aria-expanded="true"] .arw{transform:rotate(90deg)}
tbody tr.ntco .ntn{color:var(--muted);font-weight:500;white-space:nowrap;
  font-variant-numeric:tabular-nums}
tbody tr.ntco .ntdl{margin-left:auto;font-weight:600;white-space:nowrap;
  font-variant-numeric:tabular-nums}
/* 归纳形态下明细行首列留白，看得出是公司行摊开的那层；折叠中的组只藏明细行（display:none，
   数据还在 DOM 里），展开/收起不重排、轮询重渲染也不丢。
   ⚠️ 要带 #pane-notices 才压得过 :5197 那条 #pane-notices .c-name 的 padding-left:7px */
#pane-notices tbody tr.ntsub .c-name{padding-left:26px}
tbody tr.ntfold{display:none}
tbody tr.done td{opacity:.55}
tbody tr.done:hover td{opacity:.85}
select.sts{font-size:11.5px;max-width:104px}
select.sts.s-已完成{color:var(--green);border-color:var(--green);background:var(--t-green)}
select.sts.s-待处理{color:var(--orange);border-color:var(--orange);background:var(--t-orange)}
select.sts.s-已过期{color:var(--faint);background:var(--line2)}
select.sts.s-已忽略{color:var(--faint);border-style:dashed;background:var(--line2)}
select.sts.local{border-style:dashed}
/* 通知类型那一格的下拉（认不出类型时显示「待确认」，点一下选一个）。
   跟着旁边那颗状态下拉的样式走，不再另外发明一套控件。 */
select.nty{padding:2px 8px;border-radius:999px;font-size:11.5px;max-width:104px;background:var(--card);
border:1px dashed var(--line);color:var(--muted);font-family:inherit;font-weight:600;cursor:pointer;
transition:border-color .12s ease,background .12s ease}
select.nty:hover{border-color:var(--accent);color:var(--accent)}
.bar.tight{padding:9px 13px;font-size:12.5px}
.bar.tight .lbl{color:var(--muted);white-space:nowrap;font-size:12px;font-weight:600;letter-spacing:.3px}
.dl.d0{color:var(--red);font-weight:700}
.dl.d1{color:var(--red);font-weight:600}
.dl.d3{color:var(--orange);font-weight:600}
.dl.d7{color:var(--muted)}
.assoc{font-size:12px;color:var(--muted);max-width:170px}
td a{white-space:nowrap;display:inline-block;padding:3px 11px;border-radius:8px;
background:var(--accent-soft);color:var(--accent);font-size:12px;font-weight:650;
border-bottom:none;transition:background .12s ease,color .12s ease,transform .12s ease}
td a:hover{background:var(--accent);color:var(--on-accent);border-bottom:none;transform:translateY(-1px)}
.assoc .yes{color:var(--green)}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent;transition:border-color .12s ease}
a:hover{border-bottom-color:currentColor}
.del{border:none;background:transparent;color:var(--faint);cursor:pointer;font-size:15px;padding:2px 6px;border-radius:6px}
.del:hover{color:var(--red);background:var(--line2)}
.note{color:var(--muted);font-size:12.5px;margin-top:16px;line-height:1.85}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.9em;
background:var(--line2);padding:1px 6px;border-radius:6px;color:var(--fg)}
.empty{padding:40px 20px;text-align:center;color:var(--muted);font-size:13px}
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:var(--fg);color:var(--bg);
padding:10px 18px;border-radius:999px;font-size:13px;opacity:0;pointer-events:none;
transition:opacity .2s ease,transform .2s ease;z-index:9;box-shadow:var(--shadow-h)}
#toast.on{opacity:.96;transform:translateX(-50%) translateY(-2px)}
/* 页面直连邮箱的连接弹卡（她 2026-09-08 需求、2026-10-08 落地：小白也能连，不开终端）。
   浮层定式跟备注弹卡一个路数：遮罩 + 居中卡。1100px 下宽度用 min() 收着，不出滚动条。 */
#mailSetupWrap{position:fixed;inset:0;background:rgba(0,0,0,.35);display:flex;
  align-items:center;justify-content:center;z-index:60}
/* ⚠️ 这一行不能删（2026-10-08 她报「我输完连接完邮箱后之后这个还在。。」的真根因）：
   上面那条规则里的 display:flex 是**作者样式**，优先级高于浏览器默认的 [hidden]{display:none}，
   于是 closeMailSetup() 里的 hidden = true 完全不起作用 —— 弹卡带着 35% 遮罩常显，
   position:fixed + z-index:60 盖住整个视口，三个标签页全点不动。作者 display 与 [hidden]
   必须成对出现，本文件里 .form[hidden] / #appOpenAll[hidden] 都是这个写法。 */
#mailSetupWrap[hidden]{display:none}
#mailSetupCard{width:min(440px,calc(100vw - 32px));max-height:calc(100vh - 48px);overflow:auto;
  background:var(--card);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow-h);padding:16px 18px;font-size:13.5px}
#mailSetupCard label{display:block;margin:10px 0 0;font-size:12.5px;color:var(--muted)}
#mailSetupCard input,#mailSetupCard select{display:block;width:100%;box-sizing:border-box;
  margin-top:4px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
  background:var(--bg);color:var(--fg);font:inherit}
#mailSetupCard input[readonly]{color:var(--faint);background:var(--line2)}
.mshint{color:var(--muted);font-size:12.5px;margin:4px 0 0}
.mshelp{margin-top:10px;font-size:12.5px;color:var(--muted)}
.mshelp summary{cursor:pointer;color:var(--blue)}
.mshelp ul{margin:6px 0 0;padding-left:18px;line-height:1.7}
.mserr{margin-top:10px;padding:8px 10px;border-radius:8px;background:var(--t-red);
  color:var(--red);font-size:12.5px;white-space:pre-wrap}
.msacts{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
/* 「你手里这页是旧代码」提示条：服务端升级后由页面自己发现并提示（逻辑见 verTick）。
   固定右下角 —— 不挤正文，也不盖住底部居中的 toast（1080px 下两者横竖都不碰）。 */
#verNew{position:fixed;right:16px;bottom:74px;z-index:9;padding:9px 15px;border-radius:999px;
border:1px solid var(--accent);background:var(--card);color:var(--accent);font-size:12.5px;
font-family:inherit;line-height:1.5;cursor:pointer;box-shadow:var(--shadow-h)}
#verNew:hover{background:var(--accent-soft)}
#verNew b{font-weight:700}
.kpi-card.t-total b{color:var(--accent)}
.kpi-card.t-active b{color:var(--blue)}
.kpi-card.t-interview b{color:var(--purple)}
.kpi-card.t-offer b{color:var(--green)}
.kpi-card.t-closed b{color:var(--faint)}
.kpi-card.t-soon b{color:var(--orange)}
.kpi-card.t-done b{color:var(--green)}
.kpi-card.t-over b{color:var(--faint)}
.kpi-card.zero::before{opacity:.35}
.kpi-card.zero b{color:var(--muted)}
/* ---- 机会库 ---- */
#pane-opps select{max-width:152px;font-size:12.5px;padding:7px 9px}
#pane-opps input[type=search]{flex:1;min-width:190px}
/* 分布面板：一条一行，点一下就筛 */
.fbars{display:flex;flex-direction:column;gap:1px}
.fbar{display:flex;align-items:center;gap:9px;width:100%;padding:5.5px 7px;border:0;
background:transparent;border-radius:var(--r-sm);font:inherit;font-size:12.5px;color:var(--fg);
cursor:pointer;text-align:left;transition:background .12s}
.fbar:hover{background:var(--line2)}
.fbar.on{background:var(--t-blue);color:var(--blue);font-weight:650}
.fb-l{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fb-n{font-variant-numeric:tabular-nums;color:var(--muted);font-size:12px;flex:none}
.fbar.on .fb-n{color:var(--blue)}
.fb-i{position:relative;width:56px;height:6px;border-radius:3px;background:var(--line2);flex:none}
.fb-i i{position:absolute;left:0;top:0;height:100%;border-radius:3px;
background:var(--accent);opacity:.55}
.fbar.on .fb-i{background:rgba(59,130,246,.16)}
.fbmore{color:var(--faint);font-size:11.5px;padding:5px 7px 0}
/* 分布面板的「展开全部」：全表取值一条都不少，默认只铺前 10 条，展开后在框里滚 */
.fbext{display:block;width:100%;margin-top:6px;padding:5px 7px;border:0;border-top:1px dashed var(--line);
background:transparent;color:var(--blue);font-size:11.5px;font-weight:600;text-align:left;cursor:pointer;
border-radius:0}
.fbext:hover{background:var(--line2);color:var(--blue);border-color:var(--line)}
.fbwrap{max-height:268px;overflow-y:auto;overscroll-behavior:contain}
/* 选择器带上 #pane-opps，否则会被上面 input[type=search] 的通用样式盖掉 */
#pane-opps .fbin{width:100%;min-width:0;margin-bottom:6px;padding:5px 9px;font-size:12px;
border:1px solid var(--line);border-radius:var(--r-sm);background:var(--bg);color:var(--fg)}
#pane-opps .fbin:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
/* 表格里可点即筛的小标签 */
.cellf{border:0;background:transparent;padding:0;margin:0;font:inherit;color:inherit;cursor:pointer;
border-radius:var(--r-sm);text-align:left}
.cellf:hover{background:transparent;color:var(--blue)}
.cellf:hover .opp-tag,.cellf:hover .opp-tag.ind{background:var(--t-blue);color:var(--blue)}
.cellf .opp-tag{pointer-events:none}
.c-name .cellf{font-weight:700;font-size:14px}
/* 归并后这几个小尾巴会写成「26 个入口」「26 个」—— 不许在中间断行
   （投递入口那列只有 78px，「26 个入口」会折成「26 个 / 口」，看着像坏掉了）*/
.opp-more{color:var(--faint);font-size:11.5px;margin-left:2px;white-space:nowrap}
/* 行内「详情」：14 个字段一个不落地摊开 */
.detbtn{width:24px;height:24px;padding:0;line-height:1;font-size:13px;border-radius:var(--r-sm)}
.detbtn.on{background:var(--accent-soft);border-color:var(--accent);color:var(--accent)}
tr.detrow>td{background:var(--line2);border-bottom:1px solid var(--line);padding:12px 16px}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(268px,1fr));gap:6px 20px}
.ditem{font-size:12.5px;line-height:1.85;display:flex;gap:8px;align-items:baseline;min-width:0}
.ditem em{font-style:normal;color:var(--muted);flex:none;width:62px;text-align:right;font-size:11.5px}
.ditem .dv{min-width:0;word-break:break-word}
.ditem .dv a{color:var(--blue);word-break:break-all}
.dtags{display:flex;flex-wrap:wrap;gap:5px}
.dtag{border:0;padding:1.5px 9px;border-radius:999px;font-size:11.5px;font-weight:600;
background:var(--line);color:var(--muted);cursor:pointer}
.dtag:hover{background:var(--t-blue);color:var(--blue)}
.dtag.on{background:var(--accent);color:var(--on-accent)}
.qtag{flex:none;font-size:12px;font-weight:650;color:var(--muted);letter-spacing:.3px;white-space:nowrap;
padding-top:1px}
.qtag.excl{color:var(--red)}
/* 包含 / 不含 各占一整行；每行里能挂多条「字段 + 词」条件，放不下自动换行 */
.qgroup{flex:1 1 100%;display:flex;gap:9px;align-items:flex-start;min-width:0}
/* 下面那排筛选控件也自己占一整行 —— 否则中间那个「撑开」的空白会把计数挤到第四行去 */
.qfoot{flex:1 1 100%;display:flex;gap:9px;align-items:center;flex-wrap:wrap;min-width:0}
.qrows{flex:1 1 0;min-width:0;display:flex;flex-wrap:wrap;gap:7px 9px}
/* 只有一条条件时它撑满整行；两条各占一半；再多按 268px 起步换行。
   min-width 必须卡在「自己的内容宽度」上：写成 0 的话，一条条件会被压得比
   内容（字段下拉 + 输入框 + 删除钮）还窄，内容就横向溢出去，删除钮盖住旁边
   那条的下拉框、最后一条还会盖住右边的 ＋。宽度不够时应该往下换行。 */
.qrow{flex:1 1 268px;min-width:min-content;display:flex;gap:6px;align-items:center}
/* 特异性坑：上面 `#pane-opps input[type=search]{min-width:190px}` 带 id，
   会盖掉不带 id 的这条，行内输入框一直被按 190px 算 —— 一行挂三条必然溢出。
   带上 #pane-opps 才压得住；120px 是还能看清自己在打什么的下限。 */
#pane-opps .qrow > input[type=search]{flex:1 1 60px;min-width:120px}
/* 每个搜索框前面的「字段」下拉。注意要用 #pane-opps select.qf 才盖得住
   上面那条 #pane-opps select{max-width:152px}（它带一个 id，特异性更高） */
#pane-opps select.qf{flex:none;max-width:106px;padding:7px 8px;font-size:12.5px}
#pane-opps select.qf.has{border-color:var(--faint);background:var(--hover);color:var(--fg);font-weight:600}
#pane-opps select.qf.excl.has{border-color:var(--red);background:var(--t-red);color:var(--red)}
.qrow > input.has{border-color:var(--red);background:var(--t-red)}
.qadd{flex:none;width:26px;padding:4px 0;font-size:14px;font-weight:700;line-height:1.25;color:var(--muted)}
.qadd:hover{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}
.qdel{flex:none;width:22px;padding:4px 0;font-size:13px;line-height:1.25;color:var(--faint)}
.qdel:hover{color:var(--red);border-color:var(--red);background:var(--t-red)}
/* 只剩一条时不给删 —— 删到底会连框都没有，反而没法输入 */
.qrow:only-child .qdel{display:none}
.revbtn{flex:none;font-weight:600}
.revbtn.rev{background:var(--t-orange);border-color:transparent;color:var(--orange)}
.c-roles{color:var(--muted);font-size:12.5px;max-width:360px}
/* 行业名最长能到 30 多字（"商务服务/咨询/财务/审计/税务/人力/法律/翻译/知识产权"），
   标签必须允许换行，否则一格就把整张表撑宽 */
.c-ind{max-width:178px}
.c-ind .cellf{max-width:100%;white-space:normal}
.c-ind .opp-tag{white-space:normal;word-break:break-word;line-height:1.55}
.c-cities{color:var(--muted);font-size:12px;max-width:150px;line-height:1.7}
.opp-tag{display:inline-block;padding:1.5px 8px;border-radius:999px;font-size:11.5px;
font-weight:600;background:var(--line2);color:var(--muted);margin:0 3px 3px 0;white-space:nowrap}
/* 「最近新开」：行内标出最近更新过的记录 */
.fresh{display:inline-block;margin:0 0 0 6px;padding:1px 7px;border-radius:999px;
font-size:11px;font-weight:600;background:var(--t-green);color:var(--green);
vertical-align:1px;white-space:nowrap}
.fresh.now{background:var(--t-red);color:var(--red)}
.opp-tag.ind{background:var(--t-blue);color:var(--blue)}
/* 公司名下面那排小标签：招聘对象 + 企业性质，往下叠一行，不占横向空间 */
.cnats{display:flex;flex-wrap:wrap;gap:3px;margin-top:5px;white-space:normal}
.cnats .opp-tag{font-size:10.5px;padding:1px 6px;font-weight:600}
.opp-tag.tgt{background:var(--t-gray);color:var(--muted)}
.dl.open{color:var(--green);font-weight:600}
.dl.soon{color:var(--orange);font-weight:600}
.dl.none{color:var(--faint);font-size:12px}
#pane-opps td a{font-size:11.5px;padding:2.5px 10px}
.foot{margin-top:20px;color:var(--faint);font-size:11.5px;text-align:center;
letter-spacing:.2px;line-height:2}
.foot b{color:var(--muted);font-weight:650}
/* 页脚那枚「筛选条件已记住」：存取失败以前被 try/catch 静默吞掉，看不出任何痕迹，
   所以记忆状态本身要可见 —— 绿色=在记，红色=这个浏览器不让存。
   注意选择器要带 #foot：.foot{color:var(--faint)} 和 .memo 同为 (0,1,0)，
   让子元素自己的声明胜出虽然符合规则，但写明确一点更不怕以后有人调顺序。 */
#foot .memo{display:inline-block;padding:1px 10px;border-radius:999px;font-size:11px;
border:1px solid var(--green);color:var(--green);background:var(--t-green);
letter-spacing:.2px;cursor:help;white-space:nowrap}
#foot .memo.bad{border-color:var(--red);color:var(--red);background:var(--t-red)}
#foot .memo::before{content:"●";font-size:8px;margin-right:5px;vertical-align:2px}
/* ---- 面试进度（v0.21.0：从「单独一块」改成表格里的一列）----
   她的原话：「把这个进度条加到这个看板里面来，有 ai 面或者面试的才加进来，测评不加进度条，
   可手动点击进度条更改我的投递进度；排序规则，有进度条的排在前面、没进度条的排在后面」。
   所以这一列**按行**给：一条通知的「类型」是 AI面 / 面试（或邮件主题明写面试邀请）才带进度条，
   测评 / 笔试 / 资料补充 那几行留空。
   节点是这家实际走过的环节：走过的浅底、当前那一环实心、**没到的整段折成一个灰块**
   （一家 6~8 环全铺开，表宽就 1278px，装不下，她也不需要看没到的那几环具体叫啥）。 */
.ivbar{display:flex;align-items:center;flex-wrap:wrap;row-gap:6px;min-width:0}
.ivn{padding:2.5px 10px;border-radius:var(--r-pill);font-size:11.5px;font-weight:600;
white-space:nowrap;line-height:1.7;border:1px solid transparent;
/* 2026-10-08 无障碍 A1：环节从 <span> 改成原生 <button>（本来就是能点的操作）。
   抹掉 UA 默认的底色 / 字体，各种状态的底色 / 字色照旧由下面的 .ok/.cur/.next 给 */
background:none;color:inherit;font-family:inherit}
/* 已经过的环节**点一下能退回来**（她 2026-10-04：「选择进度时候也要可以回退」）。
   原来这格没有任何 handler，看着是纯装饰 —— 所以悬停必须给一个明确的「可点」反馈，
   不然她还是不会知道能点。 */
.ivn.ok{background:var(--accent-soft);color:var(--accent);cursor:pointer}
.ivn.ok:hover{background:var(--card);border-color:var(--accent);box-shadow:var(--ring)}
.ivn.cur{background:var(--accent);color:var(--on-accent);box-shadow:var(--ring);cursor:pointer}
.ivn.cur.offer{background:var(--green)}
.ivn.cur.ask{background:var(--card);color:var(--accent);border-color:var(--accent);
border-style:dashed;box-shadow:none}
.ivn.cur.ask em{font-style:normal;font-weight:700}
.ivn.next{background:var(--line2);color:var(--faint);border-style:dashed;border-color:var(--line);
cursor:pointer}
.ivn.next:hover{color:var(--muted);border-color:var(--faint)}
.ivl{display:inline-block;width:15px;height:2px;background:var(--line);flex:0 0 auto}
.ivl.ok{background:var(--accent);opacity:.45}
.ivtag{display:inline-block;padding:1px 8px;border-radius:var(--r-pill);
font-size:10.5px;font-weight:600;background:var(--t-gray);color:var(--muted)}
/* 两个可切换的视图（v0.22.0 她定的）：两套内容都常驻在 DOM 里，切换只翻 <html data-ntv>
   —— 不重新渲染，切过去零延迟、也不会闪一下空白。 */
.nttabs{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:2px 0 14px}
.nttabs button{padding:7px 15px;font-size:12.5px;font-weight:600;border-radius:var(--r-sm);
background:var(--card);border:1px solid var(--line);color:var(--muted);cursor:pointer}
.nttabs button.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent);
box-shadow:var(--ring)}
.nttabs .grow{flex:1 1 auto}
.nttabs .hint{font-size:12px;color:var(--muted);min-width:0;text-align:right}

/* 邮箱直连的提示条：没配 → 「怎么配」（信息态）；没认出公司 → 待办（琥珀态）。
   两种都不报错、都能忽略 —— 接不接邮箱是用户的选择，不是错误。 */
.mailbar{margin:0 0 14px;padding:10px 12px;border:1px solid var(--line);border-radius:var(--r-sm);
background:var(--card);color:var(--muted);font-size:12.5px;line-height:1.75}
.mailbar b{color:var(--fg);font-weight:600}
.mailbar.todo{background:var(--t-orange);border-color:transparent}
.mailbar .mrow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:5px 0;
border-top:1px dashed var(--line)}
.mailbar input[type=text]{flex:0 1 220px;min-width:140px;padding:5px 8px;font-size:12.5px;
border:1px solid var(--line);border-radius:var(--r-sm);background:var(--bg);color:var(--fg)}
.mailbar .link{color:var(--accent);cursor:pointer;background:none;border:none;padding:0;
font:inherit;text-decoration:underline}
.mailbar .link:disabled{color:var(--faint);cursor:default;text-decoration:none}
.mailbar code{font-size:12px}
html[data-ntv="dl"] #ntIvView,html[data-ntv="iv"] #ntDlView{display:none}

/* 通知表现在是 9 列（「面试进度」那一列挪去第二个视图了）。
   列宽还是 table-layout:fixed 定死 —— 靠 auto 挤永远挤不准（它按各列内容宽分余量，
   公司名那列怎么都不肯让），1080px 下会撑出横向滚动条。 */
#pane-notices table{table-layout:fixed}
#pane-notices th,#pane-notices td{padding:12px 10px}
#ntDlView thead th:nth-child(1){width:19%}    /* 公司 */
#ntDlView thead th:nth-child(2){width:10%}    /* 岗位 */
#ntDlView thead th:nth-child(3){width:8%}     /* 类型 */
#ntDlView thead th:nth-child(4){width:14.5%}  /* 截止时间 */
#ntDlView thead th:nth-child(5){width:8.5%}   /* 距截止 */
#ntDlView thead th:nth-child(6){width:11.5%}  /* 处理 */
#ntDlView thead th:nth-child(7){width:8.5%}   /* 投递状态 */
#ntDlView thead th:nth-child(8){width:6%}     /* 链接 */
#ntDlView thead th:nth-child(9){width:14%}    /* 主题 */
/* 公司名 / 日期不再「一行到底」：列宽定死了，硬撑着只会糊到隔壁格子上 */
#pane-notices .c-name{white-space:normal;word-break:break-word;padding-left:7px;padding-right:7px}
#pane-notices .dl{white-space:normal}
#pane-notices .c-topic{overflow-wrap:anywhere}
/* 已经处理掉的那几行，「距截止」只画一个破折号 —— 都做完了还挂个「过期 21 天」，
   正是她说「顶部太乱」的那类噪音（2026-10-03） */
td.dl-off{color:var(--faint)}

/* ---- 手动补的通知（v0.35.0）：行内表单 + 只在她要看时才显形的删除入口 ---- */
.addbox{margin:0 0 12px;padding:12px 14px;border:1px solid var(--line);border-radius:var(--r-sm);
background:var(--card);display:flex;flex-wrap:wrap;gap:10px 18px;align-items:flex-end}
.addbox[hidden]{display:none}
.addbox .addrow{display:flex;flex-direction:column;gap:4px}
.addbox .addrow.act{flex-direction:row;gap:8px;align-items:center;align-self:flex-end}
.addbox label{font-size:11.5px;color:var(--muted);font-weight:600}
.addbox input,.addbox select{padding:5px 8px;font-size:12.5px;border:1px solid var(--line);
border-radius:var(--r-sm);background:var(--bg);color:var(--fg);font-family:inherit}
.addbox #ntAddCo{min-width:168px}
.addbox .dlrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.addbox .quick{padding:4px 9px;font-size:11.5px;white-space:nowrap}
/* 删除入口平时不显形（这张表 1080px 下横向零余量，不能给表加列）——
   鼠标划过这一行、或键盘 Tab 到这格时才浮出来。 */
#pane-notices button.ntdel{margin-left:5px;padding:0 7px;border:1px solid var(--line);border-radius:999px;
background:var(--card);color:var(--faint);font-family:inherit;font-size:11px;line-height:1.8;
cursor:pointer;opacity:0;transition:opacity .12s ease,color .12s ease,border-color .12s ease}
#pane-notices tr:hover button.ntdel,#pane-notices tr:focus-within button.ntdel{opacity:1}
#pane-notices button.ntdel:hover,#pane-notices button.ntdel.confirm{opacity:1;color:var(--red);
border-color:var(--red)}

/* ---- 第二个视图：投递进度（一家一行，进度条铺满中间那一栏） ---- */
#ivList{border:1px solid var(--line);border-radius:var(--r-sm);background:var(--card);overflow:hidden}
.ivrow{display:grid;grid-template-columns:minmax(140px,190px) minmax(0,1fr) auto;
align-items:center;gap:14px;padding:12px 15px;border-bottom:1px solid var(--line)}
.ivrow:last-child{border-bottom:0}
.ivrow .ivco{min-width:0}
.ivrow .ivco b{display:block;font-size:13px;font-weight:650;color:var(--fg);cursor:pointer;
white-space:normal;word-break:break-word}
.ivrow .ivco b:hover{color:var(--accent);text-decoration:underline}
.ivrow .ivco span{display:block;font-size:11.5px;color:var(--muted);margin-top:3px}
/* 一键「止步」：平时跟「改」一个分量，鼠标过去才变红 ——
   这不是常规操作，做成常亮的红按钮会吓人，但点之前得看出它的分量。 */
button.ivhalt{padding:4px 10px;font-size:11.5px}
button.ivhalt:hover{color:var(--red);border-color:var(--red)}
.ivhaltnote{font-size:11.5px;color:var(--faint);white-space:nowrap}
/* 止步的整行淡下去 + 浅底：从「还在推的」里一眼分得出来
   （进度条本身也不再画后面那截灰路径了）。
   ⚠️ 淡的只能是**环节链条**：把 opacity 写在 `.ivbar` 那一层（.ivrow.stopped .ivbar）会连末尾
   那个红「已挂」一起淡掉（父级 opacity 是整体相乘，子元素自己设 opacity:1 也救不回来）——
   而那个红标正是这一行最该被看见的东西。 */
.ivrow.stopped{background:var(--line2)}
.ivrow.stopped .ivco b{color:var(--muted)}
.ivrow.stopped .ivn,.ivrow.stopped .ivl{opacity:.62}
.ivrow.stopped .ivco b:hover{color:var(--accent)}
/* 挂在进度条末尾的红标：整块列表里唯一的实心红，挂了的扫一眼就能挑出来。
   字色走 --on-red（浅色主题深红底配白字、深色主题浅红底配深字），别写死 #fff。 */
.ivdead{display:inline-block;padding:2.5px 10px;border-radius:var(--r-pill);font-size:11.5px;
font-weight:700;background:var(--red);color:var(--on-red);white-space:nowrap;
line-height:1.7;flex:0 0 auto}
/* 拒信 / Offer 邮件的凭据小字（跟在公司名下面那行里）：淡化但可点 —— 依据要么是主题、
   要么带邮件链接，链接跟正文字色走（行本身已有链接定式），只是字号压小一档 */
.ivev{color:var(--muted);font-size:12px}
.ivev a{color:var(--muted)}
#ivList .ived{margin:0;border-radius:0;border-top:1px solid var(--line);
background:var(--line2);padding:12px 16px 13px}
.ived{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:10px 12px;margin:0 0 9px;
background:var(--card);border-radius:var(--r-sm)}
.ived .lbl{font-size:11.5px;color:var(--muted)}
/* 带 #pane-notices 才压得住上面那条通用 input 规则（特异性，不是顺序问题） */
#pane-notices .ived input[type=text]{flex:1 1 220px;min-width:200px;padding:6px 11px;font-size:12.5px}
/* ---- 窄屏：别把表格挤成一团，次要列收起来 ---- */
@media (max-width:1200px){
  body{padding:22px 14px 52px}
  .tbl-wrap{border-radius:var(--r-sm)}
  #pane-notices table,#pane-apps table{min-width:0}
  /* 第 9 列 = 主题。窄屏先让出它（内容本来就在邮件里，少看一列不心疼） */
  #ntDlView th:nth-child(9),#ntDlView td:nth-child(9){display:none}
  /* 让出来的宽度补给「截止时间」和「处理」—— 这两列挤不得 */
  #ntDlView thead th:nth-child(4){width:17%}
  #ntDlView thead th:nth-child(6){width:14%}
  /* 投递进度窄屏**不再藏备注列**：备注要点着改（她屏幕就 1080px，藏了等于没这功能）。
     所以「已投 N 天」并进了「投递时间」那一格，腾出来的位置留给备注。 */
  #pane-opps th:nth-child(6),#pane-opps td:nth-child(6),
  #pane-opps th:nth-child(8),#pane-opps td:nth-child(8){display:none}
  .c-topic{max-width:none}
  .c-roles{max-width:320px}
}
@media (max-width:980px){
  .kpi{grid-template-columns:repeat(auto-fit,minmax(126px,1fr))}
  .kpi-card b{font-size:26px}
  .grid{grid-template-columns:1fr}
  /* 第 2 列 = 岗位、第 8 列 = 链接。这两列在窄屏最不占分量 */
  #ntDlView th:nth-child(2),#ntDlView td:nth-child(2),
  #ntDlView th:nth-child(8),#ntDlView td:nth-child(8){display:none}
  /* 进度视图在 980 以下改成竖排：公司名一行，进度条一行 */
  .ivrow{grid-template-columns:minmax(0,1fr);gap:9px}
  #pane-apps th:nth-child(5),#pane-apps td:nth-child(5),
  #pane-apps th:nth-child(7),#pane-apps td:nth-child(7){display:none}
  #pane-opps th:nth-child(2),#pane-opps td:nth-child(2),
  #pane-opps th:nth-child(4),#pane-opps td:nth-child(4){display:none}
  .sub{margin-left:0;text-align:left}
}
/* ---- 键盘可达性 / 滚动条 / 动效偏好：专业界面的基本功，缺了就是「没打磨」 ---- */
/* Tab 键走到哪儿要看得见；用 :focus-visible 而不是 :focus —— 鼠标点一下不该闪一圈 */
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
/* 表格横向滚动时不再用系统那根粗灰条 */
.tbl-wrap{scrollbar-width:thin;scrollbar-color:var(--line) transparent}
.tbl-wrap::-webkit-scrollbar{width:11px;height:11px}
.tbl-wrap::-webkit-scrollbar-track{background:transparent}
.tbl-wrap::-webkit-scrollbar-thumb{background:var(--line);border-radius:999px;
border:3px solid var(--card)}
.tbl-wrap::-webkit-scrollbar-thumb:hover{background:var(--faint)}
/* 暗色下强调色是浅蓝，实心块得配深字（WCAG AA）—— 这件事已经收进 --on-accent token，
   所以这里不再需要一条 @media 去单独盖 button.primary。 */
/* 系统开了「减弱动态效果」就一律不动 */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{transition-duration:.01ms!important;animation-duration:.01ms!important;
  animation-iteration-count:1!important}
}
</style>
</head>
<body>
<div class="wrap">
<header class="top">
  <div>
    <h1>秋招看板<span class="ver" id="ver"></span></h1>
    <div class="brand" id="brand"></div>
  </div>
  <div class="sub" id="sub"></div>
</header>

<div class="tabs">
  <button class="tab on" data-tab="apps">投递进度<span class="n" id="tabApps"></span></button>
  <button class="tab" data-tab="notices">邮箱通知<span class="n" id="tabNotices"></span></button>
  <button class="tab" data-tab="opps">机会库<span class="n" id="tabOpps"></span></button>
  <!-- 配色切换放在标签栏右端：不额外占一行，也躲开了标题行在窄窗口下的折行。
       做三态而不是两态 —— 她的 Mac 本身可能就在深色下，得留一个「回到跟随系统」的出口。 -->
  <div class="themebar" id="themeBar" role="group" aria-label="配色方案">
    <button type="button" data-thm="auto" title="跟随系统外观（macOS 切到深色，这里跟着变）">自动</button>
    <button type="button" data-thm="light" title="始终用浅色">浅色</button>
    <button type="button" data-thm="dark" title="始终用深色">深色</button>
  </div>
</div>

<section class="pane on" id="pane-apps">
  <div class="kpi" id="appKpi"></div>
  <div class="grid">
    <section class="panel"><h2>投递状态分布</h2><div id="appByStatus"></div></section>
    <section class="panel"><h2>投递节奏</h2><div id="appByTime"></div></section>
    <section class="panel trend"><h2>近 7 天投递</h2><div id="appTrend"></div>
      <div class="ov" id="appOverview"></div></section>
  </div>
  <div class="bar">
    <input type="search" id="appQ" placeholder="搜公司 / 岗位 / 备注">
    <span class="sep"></span>
    <span id="appStatusChips"></span>
    <button id="appReset" hidden>清除筛选</button>
    <span class="grow"></span>
    <span id="appCount" role="status"></span>
    <button id="appOpenAll" hidden>展开全部</button>
    <button id="addToggle" class="primary">+ 手动加一条</button>
    <button id="appSync">同步飞书进度</button>
    <button id="refreshBtn">刷新</button>
  </div>
  <div class="form" id="addForm" hidden>
    <input type="text" id="fCompany" placeholder="公司名称（必填）">
    <input type="text" id="fPosition" placeholder="岗位名称">
    <input type="url" id="fUrl" placeholder="网页链接">
    <select id="fStatus"></select>
    <input type="text" id="fNote" placeholder="备注">
    <button id="fSave" class="primary">保存</button>
    <button id="fCancel">取消</button>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th scope="col">公司</th><th scope="col">岗位</th><th scope="col">状态</th><th scope="col">投递时间</th>
        <th scope="col">邮箱通知</th><th scope="col">链接</th><th scope="col">备注</th>
        <th scope="col" aria-label="操作"></th>
      </tr></thead>
      <tbody id="appTb"></tbody>
    </table>
  </div>
  <div class="note">
    <b>表格里每一格都能点着改</b>：公司名 / 岗位名 / 投递时间 / 链接 / 备注 / 状态，改完立刻写进
    <code>data/applications.csv</code>，不用点保存（Enter 存、Esc 撤）。<br>
    同一家公司投了多个岗位的，会<b>折叠到公司那一行下面</b>：点公司名展开、再点收起（也能用右上角「展开全部」）。<br>
    同一家的几个子公司 / 分公司（示例五局、示例南洋、示例国际城市开发…）：<b>把一家那一行直接拖到
    另一家那一行上，松手就并过去</b> —— 拖着的时候落点那行会亮一圈，拖到「示例建筑集团」那行，几家就都收进它
    下面了。也可以<b>点右边那个「并入」</b>开个框填主公司名（表里本来没有的名字，比如第一次建「示例建筑集团」，
    就得用它）。<b>只认你自己点过 / 拖过的那条，程序不猜</b>（「示例家居」是男装、「示例家居集团」是卫浴，
    光看名字比真母子还像）。要拆开：点开主公司那一行，在子公司那行点「拆出」。<br>
    「邮箱通知」这一列是按<b>公司名确实是同一家</b>配出来的（v0.33.8 起）：名字只是「谁是谁的前两个字」
    不算同一家 —— 「京东」不会再被算到「京东方」头上。这一列改不了，要改的是那封邮件本身。<br>
    <b>飞书那张投递表是进度的真相</b>：<span id="appSyncInfo" aria-live="polite">—</span>
  </div>
</section>

<section class="pane" id="pane-notices">
  <!-- v0.22.0：这一页是**两个可切换的视图**（她定的）——
       ①「面试测评截止」= 那张通知表（进度条列已经挪走，回到干净的通知表）
       ②「投递进度」   = 按公司画进度条，只停在测评阶段的整条不列
       两块都常驻在 DOM 里，切换只翻 <html data-ntv>，不重新渲染。
       顶部原来那三块（待办条 / 六个数字 / 三块分布）她嫌乱，砍成「三个数字 + 一块分布」。 -->
  <div class="nttabs">
    <button id="ntvDl" class="on">面试测评截止</button>
    <button id="ntvIv">投递进度</button>
    <span class="grow"></span>
    <span class="hint" id="ntvHint"></span>
  </div>

  <!-- 邮箱直连的提示条：① 没配 → 给「怎么配」（不报错、可跳过）；
       ② 配了但有认不出公司的邮件 → 给一句待办 + 一键归类。见 mailBar() -->
  <div id="ntMailBar" class="mailbar" aria-live="polite" hidden></div>

  <div id="ntDlView">
    <div class="kpi" id="ntKpi"></div>
    <div class="grid">
      <section class="panel"><h2>通知类型分布</h2><div id="ntByType"></div></section>
    </div>
    <div class="bar">
      <input type="search" id="ntQ" placeholder="搜公司 / 岗位 / 主题">
      <span class="sep"></span>
      <span id="ntTypeChips"></span>
      <span class="grow"></span>
      <button id="ntReset" hidden>清除筛选</button>
      <span id="ntCount" role="status"></span>
    </div>
    <div class="bar tight">
      <span class="lbl">排序</span>
      <button id="ntSortUrg" class="on">按紧急程度</button>
      <button id="ntSortDl">按截止时间</button>
      <button id="ntSortCo">按公司</button>
      <span class="sep"></span>
      <button id="ntGrouped" class="on" title="同公司的通知归成一行，点公司行摊开明细；再点一下回到一行一封">按公司归纳</button>
      <button id="ntHideDone">隐藏已处理</button>
      <button id="ntOnlyTodo">只看没处理的</button>
      <button id="ntUrgent">只看紧急的</button>
      <span class="grow"></span><button id="ntAdd">＋ 手动添加</button>
    </div>
    <!-- 手动补通知的行内表单（不用浮层：1080px 下浮层容易溢出）。内容由 JS 现填 ——
         类型选项跟 META.mail_types 保持一致，公司候选取自 NOTICES ∪ APPS。 -->
    <div id="ntAddBox" class="addbox" hidden></div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th scope="col">公司</th><th scope="col">岗位</th><th scope="col">类型</th><th scope="col">截止时间</th><th scope="col">距截止</th>
          <th scope="col">处理</th><th scope="col">投递状态</th><th scope="col">链接</th><th scope="col">主题</th>
        </tr></thead>
        <tbody id="ntTb"></tbody>
      </table>
    </div>
    <div class="note" id="ntNote"></div>
  </div>

  <div id="ntIvView">
    <div class="kpi" id="ivKpi"></div>
    <!-- v0.24.0：这一页也能筛进度了（她 2026-10-05 提的）。
         环节那排按钮是**按当前数据现取**的 —— 没走到那一环的公司不存在，就不摆那个按钮，
         免得点开是一张空列表。 -->
    <div class="bar">
      <input type="search" id="ivQ" placeholder="搜公司 / 岗位">
      <span class="sep"></span>
      <span id="ivNodeChips"></span>
      <span class="grow"></span>
      <button id="ivReset" hidden>清除筛选</button>
      <span id="ivCount" role="status"></span>
    </div>
    <div id="ivList"></div>
    <div class="note" id="ivNote"></div>
  </div>
</section>

<section class="pane" id="pane-opps">
  <div id="oppAlert" aria-live="polite"></div>
  <div class="kpi" id="oppKpi"></div>
  <div class="grid">
    <section class="panel"><h2>行业分布</h2><div id="oppByInd"></div></section>
    <section class="panel"><h2>批次分布</h2><div id="oppByBatch"></div></section>
    <section class="panel"><h2>招聘对象</h2><div id="oppByTarget"></div></section>
    <section class="panel"><h2>企业性质</h2><div id="oppByNat"></div></section>
  </div>
  <div class="bar">
    <div class="qgroup">
      <span class="qtag">包含</span>
      <div class="qrows" id="incRows"></div>
      <button class="qadd" id="incAdd" title="再加一条「包含」条件">＋</button>
    </div>
    <div class="qgroup">
      <span class="qtag excl">不含</span>
      <div class="qrows" id="excRows"></div>
      <button class="qadd" id="excAdd" title="再加一条「不含」条件">＋</button>
    </div>
    <div class="qfoot">
      <select id="oppInd"><option value="">全部行业</option></select>
      <select id="oppCity"><option value="">全部城市</option></select>
      <select id="oppNat"><option value="">全部性质</option></select>
      <select id="oppBatch"><option value="">全部批次</option></select>
      <span class="grow"></span>
      <button id="oppSync" title="立刻去飞书拉一次最新的岗位表">检查更新</button>
      <button id="oppReset" hidden>清除筛选</button>
      <span id="oppCount" role="status"></span>
    </div>
  </div>
  <div class="bar tight">
    <span class="lbl">只看</span>
    <button id="oppOpen" class="on">还没截止</button>
    <button id="oppMine">我投过的公司</button>
    <button id="oppExam">含免笔试</button>
    <button id="oppNew" title="只看最近更新过的岗位；点一下换档位：7 天内 → 3 天内 → 今天 → 关闭">最近新开</button>
    <span class="sep"></span>
    <span class="lbl">排序</span>
    <button id="oppSortDl" class="on">按截止时间</button>
    <button id="oppSortUp">按更新时间</button>
    <button id="oppSortCo">按公司名</button>
    <button id="oppSortInd">按行业</button>
    <button id="oppRev" class="revbtn" title="把当前排序反过来">↑ 正序</button>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th scope="col">公司</th><th scope="col">行业</th><th scope="col">招聘岗位</th><th scope="col">地点</th>
        <th scope="col">截止时间</th><th scope="col">批次</th><th scope="col">投递入口</th><th scope="col">公告</th>
        <th scope="col" aria-label="操作"></th>
      </tr></thead>
      <tbody id="oppTb"></tbody>
    </table>
  </div>
  <div class="bar tight" id="oppMoreBar" hidden>
    <span class="grow"></span>
    <button id="oppMoreBtn" class="primary">再看 60 家</button>
    <span class="grow"></span>
  </div>
  <div class="note" id="oppNote"></div>
</section>
<div class="foot" id="foot"></div>
</div>
<div id="toast" aria-live="polite"></div>

<!-- 页面直连邮箱的连接弹卡（她 2026-10-08：「小白也能连」）。原生 dialog 式写法：
     button / label / details 全是原生控件，无障碍白拿。样式见 #mailSetupWrap 那段。 -->
<div id="mailSetupWrap" hidden>
  <div id="mailSetupCard" role="dialog" aria-modal="true" aria-label="连接我的邮箱">
    <b>连接你的邮箱</b>
    <p class="mshint">填邮箱地址和授权码，看板自己收校招通知。数据只在你电脑里，不往外发。</p>
    <label for="mstEmail">邮箱地址</label>
    <input id="mstEmail" type="text" placeholder="you@qq.com" autocomplete="off">
    <label for="mstCode">授权码（IMAP）</label>
    <input id="mstCode" type="password" autocomplete="off">
    <details class="mshelp">
      <summary>怎么拿授权码？</summary>
      <ul>
        <li><b>QQ / Foxmail</b>：mail.qq.com → 设置 → 账户 → 开启 IMAP/SMTP 服务 → 短信验证 → 弹窗里的 16 位码</li>
        <li><b>163 / 126</b>：设置 → POP3/SMTP/IMAP → 开启 → 生成「客户端授权密码」</li>
        <li><b>Gmail</b>：先开两步验证 → 生成「应用专用密码」（选「邮件」）</li>
        <li><b>其他 / 学校邮箱</b>：问学校 IT 或邮箱服务商；在「服务商」里选「其他邮箱」手填服务器</li>
      </ul>
    </details>
    <label for="mstProv">服务商</label>
    <select id="mstProv">
      <option value="auto">按邮箱地址自动推断</option>
      <option value="other">其他邮箱（手填服务器）</option>
    </select>
    <label for="mstHost">IMAP 服务器</label>
    <input id="mstHost" type="text" readonly placeholder="填邮箱地址后自动推断">
    <label for="mstPort">端口</label>
    <input id="mstPort" type="text" readonly value="993">
    <div id="mstErr" class="mserr" hidden></div>
    <div class="msacts">
      <button id="mstCancel">取消</button>
      <button id="mstGo" class="primary">连接</button>
    </div>
  </div>
</div>
<!-- 页面自己发现「我这份代码过期了」时亮出来的刷新入口（平时 hidden） -->
<button id="verNew" hidden></button>
<script>
const DATA = __DATA__;
// 离线样例（看板.html）里塞一份机会库快照，免得离线打开时空着；起服务时这里是 null
const OPP_SEED = __OPPS__;
// 双击打开的离线文件没法连后端：别再定时去 fetch（只会刷出一串 CORS 报错）
const OFFLINE = (location.protocol === "file:");
const META = DATA.meta || {};
let APPS = DATA.apps || [], NOTICES = DATA.notices || [];
// 面试进度：按**公司**聚合（不是按通知）。它是**一列**，画在通知表里带 AI面 / 面试 的那几行上，
// 见 renderNotices 里的 ivCell / bindIvTable
let IVS = DATA.interview || [];
// 「并入」表：子公司名（归一后的键）→ 主公司名；空串 = 明确不并了。
// 两份：她点过的（APP_PARENT）+ 内置打底（APP_PARENT_BUILTIN，她拆一次就盖住一条）。
let APP_PARENT = DATA.app_parents || {};
const APP_PARENT_BUILTIN = DATA.app_parent_builtin || {};
const STATUSES = META.statuses || ["已投递","简历通过","测评笔试","面试","Offer","已结束"];
const ST_ORDER = STATUSES;
const ST_COLOR = {"已投递":"var(--blue)","简历通过":"var(--blue)","测评笔试":"var(--orange)",
                  "面试":"var(--purple)","Offer":"var(--green)","已结束":"var(--faint)"};
const TYPE_COLOR = {"资料补充":"var(--faint)","AI面":"var(--purple)","笔试":"var(--orange)",
                    "测评":"var(--blue)","面试":"var(--green)","拒信":"var(--red)",
                    "Offer":"var(--green)","状态更新":"var(--faint)"};
const TYPE_ORDER = ["资料补充","AI面","笔试","测评","面试","拒信","Offer","状态更新"];

// ---------------- 「我手里这页是不是旧代码」 ----------------
// 她那个标签页常常是几小时前打开的（页脚那行版本只在加载时渲染一次），我这边升级了服务、
// 她那边照旧跑老代码 —— 表现就是「你改了？我怎么没看见」，然后我反复说「硬刷新一下」，很累。
// 好在 /api/board 每 10 秒轮询都会带回 META.version（**服务端当前版本**），拿它跟「这一页
// 自己的版本」比一下就知道手里是不是旧代码：不一致 → 右下角出提示条；只要她没在敲东西，
// 就自动刷新一次（滚动位置给放回去）。
// ⚠️ PAGE_VERSION 必须是 let：board-check 会临时改它来验证「提示条出现」和「编辑中不刷新」。
let PAGE_VERSION = "__APPVER__";
let verReloaded = false;                 // 自动刷新只做一次，别刷成循环
function verStale(){
  return !!(!OFFLINE && META.version && PAGE_VERSION && META.version !== PAGE_VERSION);
}
function verBusyEdit(){
  // 正在改某一格（td.editing）或光标在输入框里 —— 此刻刷页面等于把她的输入吞掉。
  // 这一条同时是 board-check 的把手：给任意一个 td 挂上 editing 就能断言「不刷新」。
  if(document.querySelector("td.editing")) return true;
  const t = (document.activeElement && document.activeElement.tagName) || "";
  return t === "INPUT" || t === "TEXTAREA" || t === "SELECT";
}
function verCanReload(){ return !verReloaded && !document.hidden && !verBusyEdit(); }
function verDoReload(){                  // 单独一层，好让 board-check 打桩（否则只能真刷新）
  try{ sessionStorage.setItem("at.reload.y", String(window.scrollY || 0)); }catch(e){ /* 无痕模式 */ }
  location.reload();
}
function verTick(){
  const box = document.getElementById("verNew");
  if(!box) return;
  if(!verStale()){ box.hidden = true; verReloaded = false; return; }
  box.hidden = false;
  box.innerHTML = "看板已更新到 <b>v" + esc(META.version) + "</b>，点这里刷新";
  if(verCanReload()){
    verReloaded = true;
    setTimeout(verDoReload, 900);        // 留一点时间让她看见提示条
  }
}
(function verInit(){
  const b = document.getElementById("verNew");
  if(b) b.onclick = verDoReload;
  // 自动刷新回来后把滚动位置放回去（她常常正看着下面某一行）
  try{
    const y = sessionStorage.getItem("at.reload.y");
    if(y === null) return;
    sessionStorage.removeItem("at.reload.y");
    const yy = Number(y) || 0;
    window.scrollTo(0, yy);
    window.addEventListener("load", () => window.scrollTo(0, yy));
  }catch(e){ /* 没有就没有 */ }
})();

const iso = s => String(s || "").replace(" ", "T");
function ts(dl){ const t = new Date(iso(dl)).getTime(); return isNaN(t) ? NaN : t; }
function daysLeft(dl){
  const t = ts(dl);
  if(isNaN(t)) return null;
  const d = new Date(t), n = new Date();
  return Math.round((Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())
                   - Date.UTC(n.getFullYear(), n.getMonth(), n.getDate())) / 86400000);
}
function isOverDl(dl){
  const d = daysLeft(dl);
  if(d === null) return false;
  return d < 0 || (d === 0 && ts(dl) < Date.now());
}
function daysSince(s){
  const t = ts(s);
  if(isNaN(t)) return null;
  return Math.max(0, Math.floor((Date.now() - t) / 86400000));
}
function esc(s){ return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function say(msg){ const el = document.getElementById("toast"); el.textContent = msg; el.classList.add("on");
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("on"), 2200); }

// ---------------- 筛选条件记忆（三个标签页共用一套） ----------------
// 每个标签页的搜索词 / 下拉 / 快筛 / 排序都存进浏览器 localStorage：刷新、关掉再开都还在。
// 三条硬约束，别改：
//   1) 先探一次能不能存（LS_OK）。隐身模式、站点数据被禁、部分 file:// 场景下
//      localStorage 会直接抛错，以前 try/catch 一吞就变成「用起来没效果、还查不出原因」。
//   2) 读回来的每一项都验型/验白名单。存档是上一版写的、或者被人手改过，
//      坏值宁可丢掉 —— 一个不存在的筛选项等于把整张表筛成空的。
//   3) 恢复必须发生在**第一次渲染之前**，保存是挂在渲染函数里的：反过来就是
//      拿一屏默认条件把存档冲掉。每个 save 前面都有 loaded 闸门兜底。
const LS_KEY_PROBE = "at.probe";
const LS_OK = (() => {
  try{
    localStorage.setItem(LS_KEY_PROBE, "1");
    localStorage.removeItem(LS_KEY_PROBE);
    return true;
  }catch(e){ return false; }
})();
const _lsLast = {};
function lsSave(key, obj){
  if(!LS_OK) return;
  let s;
  try{ s = JSON.stringify(obj); }catch(e){ return; }
  // 内容没变就不写。保存挂在渲染函数上，而渲染每 10 秒被自动刷新碰一次 ——
  // 不比对的话就是每 10 秒一次无意义写入，而且**开着两个标签页时会互相盖**：
  // B 页没有任何筛选，它的刷新会把 A 页刚存的筛选覆盖成空的。
  if(_lsLast[key] === s) return;
  _lsLast[key] = s;
  try{ localStorage.setItem(key, s); }
  catch(e){ /* 配额满 / 被策略拒；页脚那枚小标已经说明状态了，这里不再打扰 */ }
}
function lsLoad(key){
  if(!LS_OK) return null;
  try{
    const d = JSON.parse(localStorage.getItem(key) || "null");
    return (d && typeof d === "object") ? d : null;
  }catch(e){ return null; }
}
// 页脚那枚小标：让「记不记住」这件事看得见
function memoChip(){
  return LS_OK
    ? '<span class="memo" title="三个标签页的搜索词 / 下拉 / 快筛 / 排序都记在这台浏览器里，刷新、关掉再开都还在；点各页的「清除筛选」就清空">筛选条件已记住</span>'
    : '<span class="memo bad" title="这个浏览器不让网页存本地数据（隐私模式 / 站点数据被禁），筛选条件刷新后会丢">本浏览器不记筛选</span>';
}

// ---------------- 配色：自动 / 浅色 / 深色 ----------------
// 真正决定用哪套色的是 <head> 里那段 bootstrap（它先于样式表跑，刷新不闪白）。
// 这里只管两件事：按钮选中态跟上、点了以后改属性并存起来。
// 「自动」= 移除 data-theme，交回给 CSS 的 prefers-color-scheme —— 别在这儿把
// 系统偏好解析成一个固定值写死，那样系统白天黑夜自动切换时页面就不跟了。
const THEME_KEY = "at.theme.v1";
const THEME_MODES = ["auto", "light", "dark"];
function themeApply(mode, persist){
  if(THEME_MODES.indexOf(mode) < 0) mode = "auto";
  const r = document.documentElement;
  r.setAttribute("data-theme-mode", mode);
  if(mode === "auto") r.removeAttribute("data-theme");
  else r.setAttribute("data-theme", mode);
  const bar = document.getElementById("themeBar");
  if(bar) for(const b of bar.querySelectorAll("button[data-thm]")){
    const on = b.dataset.thm === mode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  if(persist) lsSave(THEME_KEY, {mode: mode});
}
function themeInit(){
  const bar = document.getElementById("themeBar");
  if(bar) bar.addEventListener("click", (e) => {
    const b = e.target.closest ? e.target.closest("button[data-thm]") : null;
    if(b) themeApply(b.dataset.thm, true);
  });
  // 跟着 <head> 那段已经定下来的模式把按钮点亮
  themeApply(document.documentElement.getAttribute("data-theme-mode") || "auto", false);
}
function bars(el, items){
  const max = Math.max(1, ...items.map(i => i[1]));
  el.innerHTML = items.map(i => {
    const w = i[1] ? Math.max(7, Math.round(i[1] / max * 100)) : 0;
    return '<div class="brow' + (i[1] ? "" : " zero") + '">' +
      '<span class="bl">' + esc(i[0]) + '</span>' +
      '<span class="bt"><i style="width:' + w + '%;background:' + i[2] + '"></i></span>' +
      '<span class="bn">' + i[1] + '</span></div>';
  }).join("");
}
function kv(label, val){ return '<div class="kv"><span>' + esc(label) + '</span><b>' + esc(val) + '</b></div>'; }

// 近 7 天投递趋势：竖条，最右一根是今天。
// 口径跟这一页其它面板完全一致 —— 「一个投递」= 一家公司（归并过的主公司算一行），
// 取这家**最近一次**投递那天（跟「投递节奏」「最近一次投递」用的是同一把尺）。
// 画不进这 7 天的（更早的、没填投递时间的）**不硬塞一根「更早」柱子** ——
// 那会让人以为那天真投了；那 N 家由调用方写进底下那块的 title 里。
// 每根柱子 title 里列当天投了哪几家，鼠标停一下就看得见。
// ⚠️ 只画柱子，一个字都不往图下面写（她 2026-10-10：「不要下面的文字」）。
function trendChart(el, days){
  const max = Math.max(1, ...days.map(d => d.n));
  const last = days.length - 1;
  const cols = days.map((d, i) => {
    const h = d.n ? Math.max(8, Math.round(d.n / max * 100)) : 0;
    const tip = d.label + "（周" + d.dow + "）投了 " + d.n + " 家"
      + (d.cos.length ? "：" + d.cos.slice(0, 10).join("、") + (d.cos.length > 10 ? " 等" : "") : "");
    return '<div class="tcol' + (d.n ? " has" : "") + (i === last ? " today" : "") +
      '" title="' + esc(tip) + '">' +
      '<span class="tnum">' + d.n + '</span>' +
      '<span class="tt"><i style="height:' + h + '%"></i></span>' +
      '<span class="tl">' + esc(d.label) + '</span></div>';
  }).join("");
  el.innerHTML = '<div class="tchart">' + cols + '</div>';
}

// ---------------- 标签切换 ----------------
function switchTab(name, push){
  const t = document.querySelector('.tab[data-tab="' + name + '"]');
  if(!t) return;
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x === t));
  document.querySelectorAll(".pane").forEach(p =>
    p.classList.toggle("on", p.id === "pane-" + name));
  if(name === "opps") ensureOpps();
  if(push !== false){
    // 地址栏带个 #opps，刷新后还停在这一页，链接也能直接分享
    try{ history.replaceState(null, "", "#" + name); }catch(e){}
  }
}
document.querySelectorAll(".tab").forEach(t => {
  t.onclick = () => switchTab(t.dataset.tab);
});
// ---------------- 刷新后停在原页 ----------------
// 这里只定义、不调用：OPP / oppXxx 这些状态是 const 声明在脚本后半段的，
// 在这一行立刻执行 switchTab("opps") → ensureOpps() 会撞暂时性死区
// （"Cannot access 'OPP' before initialization"），整页 JS 直接停摆。
// 所以调用点必须在**整个脚本末尾**。自检里有断言守住这个顺序。
function restoreTab(){
  const h = (location.hash || "").replace("#", "");
  if(["apps", "notices", "opps"].indexOf(h) >= 0) switchTab(h, false);
}

// ---------------- 投递进度 ----------------
let appQuick = "all", appStatus = null, appQ = "";

// —— 投递进度的筛选记忆（搜索词 / 快筛 / 状态）——
// 以前只有机会库记得住，投递进度和邮箱通知的搜索框刷新就清空 —— 而「搜某个公司」恰恰是
// 这两页最常用的动作。现在三页统一走 lsSave / lsLoad 这一套。
const APP_QKEY = "at.apps.q.v1";
const APP_QUICKS = ["all", "active", "interview", "offer", "closed"];
let appQLoaded = false;
function appSave(){
  if(!appQLoaded) return;
  lsSave(APP_QKEY, {q: appQ, quick: appQuick, status: appStatus});
  // 公司折叠的展开状态跟筛选一起存（共用一个「恢复完之前不许写」的闸门，
  // 免得渲染先跑一次、拿默认值把存档冲掉）。
  const keys = {};
  Object.keys(appOpenKeys).slice(-400).forEach(k => { keys[k] = !!appOpenKeys[k]; });
  lsSave(APP_OKEY, {all: !!appOpenAll, keys: keys});
}
function appLoad(){
  const d = lsLoad(APP_QKEY);
  appQLoaded = true;
  if(!d) return;
  if(typeof d.q === "string") appQ = d.q;
  if(APP_QUICKS.indexOf(d.quick) >= 0) appQuick = d.quick;
  // status 只能是状态名白名单里的值，或者 null（＝不按状态筛）
  if(d.status === null || ST_ORDER.indexOf(d.status) >= 0) appStatus = d.status;
  const el = document.getElementById("appQ");
  if(el) el.value = appQ;
}
appLoad();   // 必须排在第一次 renderApps() 之前

function appGroup(r){
  const s = r["状态"] || "已投递";
  if(s === "已结束") return "closed";
  if(s === "Offer") return "offer";
  if(s === "面试") return "interview";
  return "active";
}

// ---------------- 同一家公司投了多个岗位 → 归到一块 ----------------
// 她投的公司里有不少是「一家投好几个岗位」（管培生 / 不同条线各报一次）。平铺着看，
// 同一家公司重复出现好几行：看不出这家一共投了几个岗位，「总投递」也被岗位数灌了水。
// 所以这里定三条口径：
//   1) 按公司名（去掉空白后）归组，多岗位的折成「公司主行 + 岗位子行」，默认收起；
//   2) **KPI / 分布 / 概览一律按公司算** —— 一家公司就是一个「投递」，它的进度取
//      组里最靠后的那个状态（Offer > 面试 > 测评笔试 > … > 已结束）；
//   3) 只投了一个岗位的公司不折叠，还是原来那一行，省得为了看一条还要点一下。
// 筛选（快筛 / 状态 / 搜索）仍然作用在**岗位**上，再把筛出来的按公司归组 ——
// 所以搜「运营」时，某家只显示命中的那几个岗位，而不是把它所有岗位都摊开。
const NO_CO = "（未填公司）";
// 公司名归一：同一家公司的写法常常不止一种 —— 扩展抓的是「恒瑞达机械」，飞书表里是
// 「深圳市恒瑞达机械有限公司」；或者「星桥科技」和「星桥科技（中国）有限公司」。
// 不归一，同一家就会散成两行，"归到一块"就白做了。这里只做**最保守**的三步：
//   去括号备注 → 去空白 → 去尾部的公司组织形式（有限公司 / 集团 …）→ 去开头的省 / 市。
// 特意不做「一个名字包含另一个就算同一家」那种松规则：那会把「长河银行」和
// 「长河银行保险」并成一家，比不合并更糟。合不上就分开显示，宁可她手动改名字。
function coNorm(s){
  const raw = String(s == null ? "" : s).replace(/\s+/g, "");
  let t = raw.replace(/[（(][^）)]*[）)]/g, "");
  for(let i = 0; i < 3; i++)
    t = t.replace(/(股份有限公司|有限责任公司|集团有限公司|控股集团|有限公司|股份公司|分公司|集团|控股|公司)$/, "");
  t = t.replace(/^(中国|[\u4e00-\u9fa5]{2,3}省|[\u4e00-\u9fa5]{2,3}市)/, "");
  return t || raw;   // 全被剥光的（名字本身就是「有限公司」这种）退回原名，别都并到一起
}
// 组里「最靠后的状态」= 这家的整体进度。已结束排在所有进度之后，
// 不能因为它在 ST_ORDER 末尾就压过 Offer。
const ST_RANK = (() => {
  const m = {};
  ST_ORDER.forEach((s, i) => { m[s] = i + 1; });
  m["已结束"] = 0;
  return m;
})();
function bestOf(rows){
  let best = (rows[0] && rows[0]["状态"]) || "已投递";
  rows.forEach(r => {
    const s = r["状态"] || "已投递";
    if((ST_RANK[s] || 0) > (ST_RANK[best] || 0)) best = s;
  });
  return best;
}
// 汇总一家的邮箱通知：条数相加、类型并集、截止时间取「最近一个还没过的；全过了就取最晚那个」
function envMerge(rows){
  let count = 0; const types = [];
  let best = "", bestT = NaN, late = "", lateT = NaN;
  rows.forEach(r => {
    const e = r["关联"] || {};
    if(!e.count) return;
    count += e.count;
    (e.types || []).forEach(t => { if(types.indexOf(t) < 0) types.push(t); });
    if(!e.next) return;
    const t = ts(e.next);
    if(isNaN(t)) return;
    if(t > Date.now()){ if(isNaN(bestT) || t < bestT){ bestT = t; best = e.next; } }
    if(isNaN(lateT) || t > lateT){ lateT = t; late = e.next; }
  });
  return {count: count, types: types, next: isNaN(bestT) ? late : best};
}
function buildGroups(rows){
  const m = new Map();
  rows.forEach(r => {
    const raw = String(r["公司名称"] == null ? "" : r["公司名称"]).trim();
    const k = coNorm(raw) || NO_CO;
    let g = m.get(k);
    if(!g){ g = {key: k, name: raw || NO_CO, rows: []}; m.set(k, g); }
    // 组名取最完整的那个写法（「深圳市恒瑞达机械有限公司」比「恒瑞达机械」清楚）
    else if(raw.length > g.name.length) g.name = raw;
    g.rows.push(r);
  });
  const gs = [...m.values()];
  gs.forEach(g => {
    g.rows.sort((a, b) => (ts(b["投递时间"]) || 0) - (ts(a["投递时间"]) || 0));
    g.best = bestOf(g.rows);
    g.group = appGroup({"状态": g.best});
    g.lastAt = g.rows[0]["投递时间"] || "";
    g.env = envMerge(g.rows);
  });
  gs.sort((a, b) => (ts(b.lastAt) || 0) - (ts(a.lastAt) || 0));
  return gs;
}

// ---------------- 子公司 → 主公司（表里那个「并入」） ----------------
// 她 2026-10-07 提的：示例五局 / 示例南洋 / 示例国际城市开发 这几行，收在「示例建筑集团」一行下面。
// **程序不猜谁是子公司**：「中国」开头的大公司一堆（四大行、邮储、兵器、航发这类互不相干的），按前缀并
// 就全糊成一家；「示例家居」（男装）跟「示例家居集团」（卫浴）更是长得比真母子还像。所以只认她点过的
// 那条（存 data/app_parents.json）。合成之后这张表的形状是**两层**：
//   主公司一行（写着「N 家子公司 · N 个岗位」）→ 点开是各家子公司 / 岗位行 → 子公司那一行还能再点开。
// 计数口径跟着改：KPI / 徽标「家数」按**行**算（主公司算一行，就是她一行行数得出来的数），
// 概览里另外报「涉及公司」的原始家数，两个数都在。
const PKEY = "@";        // 主公司那一层的键前缀，跟公司的 coNorm 键分开命名空间，免得撞车

// 查「这家的主公司是谁」：先她点过的，再打底表；归一键和显示名都试一遍。
// ⚠️ 空串是**有意义的值**（明确不并了），不能用 `||` 那种写法顺手吞掉。
function parentLabelOf(key, name){
  const tries = [key, name, coNorm(name || ""), coNorm(key || "")];
  const books = [APP_PARENT, APP_PARENT_BUILTIN];
  for(const book of books)
    for(const k of tries)
      if(k && Object.prototype.hasOwnProperty.call(book, k)) return book[k];
  return "";
}
// 沿父链上溯到最顶上那家 —— 支持「A 并到 B、B 又并到 C」，最多 20 跳
// （原来写 6 跳，现实里已有一条 9 层的链，上溯到第 6 跳就停 → 断点那层被当成顶层，计数错位；
//  环由下面的 seen 检测兜住，20 只是防超长退化链的硬上限。见 ADR-007）
function rootOfComp(g){
  let label = parentLabelOf(g.key, g.name);
  if(!label) return null;
  const seen = [PKEY + coNorm(label)];
  for(let i = 0; i < 20; i++){
    const up = parentLabelOf(PKEY + coNorm(label), label);
    if(!up) break;
    const nk = PKEY + coNorm(up);
    if(seen.indexOf(nk) >= 0) break;
    seen.push(nk);
    label = up;
  }
  return {label: label, key: PKEY + coNorm(label)};
}
// 顶层节点：没归并的 = 一家公司；归并过的 = 主公司 + 它下面那几家子公司。
// 筛 / 排仍旧作用在**岗位**上（跟以前一样），所以搜索时仍只看命中的岗位。
function buildNodes(rows){
  const comps = buildGroups(rows);
  const nodes = new Map();
  comps.forEach(g => {
    const root = rootOfComp(g);
    const key = root ? root.key : "·" + g.key;      // 没归并的：自成一个节点
    let n = nodes.get(key);
    if(!n){ n = {key: key, label: root ? root.label : g.name, wantMerge: !!root, comps: []}; nodes.set(key, n); }
    n.comps.push(g);
  });
  let out = [...nodes.values()];
  // 主公司自己也有记录的话，把它从「它自己那个节点」里摘出来、塞进主公司节点当「自己那家」——
  // 不然表里会冒出**两行同名**（内置打底那条最容易撞上：她表里既有「TCL」又有「TCL实业」，
  // 并完之后底下应该只有一行 TCL，而不是 TCL 一行 + TCL（1 家子公司）一行）。
  const eaten = [];
  out.forEach(n => {
    if(!n.wantMerge) return;
    const k = coNorm(n.label);
    const m = out.filter(x => x !== n && !x.wantMerge && x.comps.length === 1 &&
                              eaten.indexOf(x) < 0 && coNorm(x.comps[0].name) === k)[0];
    if(m){ n.comps.push(m.comps[0]); eaten.push(m); }
  });
  if(eaten.length) out = out.filter(n => eaten.indexOf(n) < 0);
  out.forEach(n => {
    n.comps.sort((a, b) => (ts(b.lastAt) || 0) - (ts(a.lastAt) || 0));
    // 主行就是她自己那家（名字归一后跟主公司名一样）→ 它的岗位不再重复写公司名
    n.own = n.wantMerge ? (n.comps.filter(c => coNorm(c.name) === coNorm(n.label))[0] || null) : null;
    n.subs = n.comps.filter(c => c !== n.own);
    n.merged = n.wantMerge && n.subs.length > 0;
    if(!n.merged){                       // 没并进任何一家 → 原样退回一家公司
      n.own = n.comps[0]; n.subs = []; n.label = n.own.name;
    }
    n.rows = n.comps.reduce((a, c) => a.concat(c.rows), []);
    n.best = bestOf(n.rows);
    n.group = appGroup({"状态": n.best});
    n.lastAt = n.comps.reduce((m, c) => ((ts(c.lastAt) || 0) > (ts(m) || 0) ? c.lastAt : m), n.comps[0].lastAt);
    n.env = envMerge(n.rows);
  });
  out.sort((a, b) => (ts(b.lastAt) || 0) - (ts(a.lastAt) || 0));
  return out;
}
// 一个节点的「汇总」长这样，子公司那一行要单独当主体画，就照这个形状造一个
function nodeOfComp(c){
  return {key: c.key, label: c.name, merged: false, own: c, subs: [], comps: [c], rows: c.rows,
          best: c.best, group: c.group, lastAt: c.lastAt, env: c.env};
}
function mergedSubs(nodes){ return nodes.reduce((s, n) => s + (n.merged ? n.subs.length : 0), 0); }

// 展开状态：默认收起（一屏先看清「一共投了几家公司」），单家点开的覆盖记在 keys 里。
// 存 localStorage，跟另外三个页签的筛选一个路子；搜索时自动摊开看得到命中的岗位。
const APP_OKEY = "at.apps.open.v1";
let appOpenAll = false, appOpenKeys = {};
function appOpenOf(k){ return (k in appOpenKeys) ? appOpenKeys[k] : appOpenAll; }
function appOpenLoad(){
  const d = lsLoad(APP_OKEY);
  if(!d) return;
  appOpenAll = !!d.all;
  if(d.keys && typeof d.keys === "object")
    Object.keys(d.keys).forEach(k => { appOpenKeys[k] = !!d.keys[k]; });
}
appOpenLoad();   // 跟 appLoad 一样，必须在第一次 renderApps() 之前

// 表格里「点一下就地改」的格子：data-f 的值 → 记录字段 / 接口参数 / 提示语。
// 只有**真的改得了**的才列进来 ——「邮箱通知」是跟邮箱看板配对算出来的，
// 不属于这张表（要改的是那封邮件本身），所以它那格保持只读。
const APP_CELLS = {
  company:  {col:"公司名称", api:"company",    tip:"点一下改公司名",   ph:"公司名称（必填）", label:"公司名"},
  position: {col:"岗位名称", api:"position",   tip:"点一下改岗位名",   ph:"岗位名称",        label:"岗位名"},
  when:     {col:"投递时间", api:"applied_at", tip:"点一下改：有「今天 / 昨天 / 前天」快捷键，也能选日历，不用手打", ph:"2026-10-04 13:34", kind:"when", label:"投递时间"},
};
function cellHtml(f, r){
  const c = APP_CELLS[f], v = String(r[c.col] || "");
  const add = {company:"＋公司", position:"＋岗位", when:"＋时间"}[f] || "＋";
  // 2026-10-08 无障碍 A1：这一格是**能点的操作**（点一下就地改），原来是 <span> 挂 onclick ——
  // 键盘 Tab 不到、回车也触发不了，读屏更不会说它是个按钮。改成原生 <button>：
  // 焦点、Enter/Space、读屏的「按钮」角色全部白拿，不用自己再写一套键盘监听。
  // `type="button"` 必须带 —— 虽然 <table> 里没有 <form>，但默认值是 submit，防以后有人把表格包进表单。
  return '<button type="button" class="cellv" data-f="' + f + '" data-v="' + esc(v) + '" title="' + esc(c.tip) + '">' +
    (v ? esc(v) : '<span class="celladd">' + add + '</span>') + '</button>';
}
// 「并入」小按钮。**竖着堆在 × 上面**，不是并排 —— 这张表在 1080px 下已经顶到容器边
// （实测列宽合计 1050 = 容器 1050，横向一个像素余量都没有），并排加 2 个字必然撑出横向滚动条。
// 操作那格本来就只放着一个 27px 的 ×、行高 71px，上面还有小半格空的。
// 已经并过的显示「拆出」，点一下就能拆回来。
function mergeBtnHtml(comp){
  if(!comp) return "";                       // 不是「一家公司」的行（岗位子行）不给
  const p = parentLabelOf(comp.key, comp.name);
  return '<button class="mg' + (p ? " on" : "") + '"' +
    ' data-mg="' + esc(comp.key) + '" data-mgname="' + esc(comp.name) + '"' +
    ' data-mgparent="' + esc(p) + '"' +
    ' title="' + esc((p ? ("现在并在「" + p + "」下面 —— 点一下拆出来")
                        : "把这家并到主公司下面（比如 示例五局 → 示例建筑集团）") +
                      "；也可以直接把这一行拖到另一家公司那一行上") + '">' +
    (p ? "拆出" : "并入") + '</button>';
}

// 一条岗位记录一行。sub=true 时是某个公司主行下的子行：第一列让给连接线，
// 岗位名加粗当识别主体（公司名就在上面那行，不重复写）。
// 归并过的组里，子行还要**写清是哪家子公司**（subName，只在每家第一条上写一次）——
// 不然拆开一看十几行，分不清哪条是哪家的。
// comp 传了就给这一行挂「并入 / 拆出」按钮：只有**代表一家公司**的行才传
// （独立公司的唯一那行、归并组里只有一条岗位的子公司），岗位子行不传。
function subLabel(name){
  return '<span class="subdot"></span>' +
    (name ? '<span class="subco" title="' + esc(name) + '">' + esc(name) + '</span>' : '');
}
function appRow(r, sub, subName, comp){
  const env = r["关联"] || {count:0, types:[], next:""};
  const d = daysLeft(env.next);
  const over = isOverDl(env.next);
  const cls = env.next ? (over ? "over" : d === 0 ? "d0" : d <= 1 ? "d1" : d <= 3 ? "d3" : d <= 7 ? "d7" : "") : "";
  const dlTxt = !env.next ? "—" : over ? "已过期" : d === 0 ? "今天" : d + " 天";
  const since = daysSince(r["投递时间"]);
  const tr = document.createElement("tr");
  tr.className = (sub ? "sub" : "") + (r["状态"] === "已结束" ? " closed" : "");
  tr.innerHTML =
    (sub ? '<td>' + subLabel(subName) + '</td>'
         : '<td class="c-name">' + cellHtml("company", r) + '</td>') +
    '<td class="c-pos">' + cellHtml("position", r) + '</td>' +
    '<td><select class="st st-' + esc(r["状态"]) + '" data-id="' + esc(r["id"]) + '">' +
      ST_ORDER.map(s => '<option' + (s === r["状态"] ? " selected" : "") + '>' + esc(s) + '</option>').join("") +
    '</select></td>' +
    '<td class="dl">' + cellHtml("when", r) +
      (since === null ? "" : '<small>' + since + ' 天前</small>') + '</td>' +
    '<td class="assoc">' + (env.count
        ? '<span class="yes">' + env.count + ' 条</span> · ' + env.types.map(t => esc(t)).join("/") +
          '<br><span class="dl ' + cls + '">最近截止 ' + dlTxt + '</span>'
        : '<span style="color:var(--faint)">暂无</span>') + '</td>' +
    '<td' + (r["网页链接"] ? ' class="haslink"' : '') + '>' + (r["网页链接"]
        ? '<a href="' + esc(r["网页链接"]) + '" target="_blank" rel="noopener">打开</a>' +
          '<button class="lnkedit" title="改这个链接">改链接</button>'
        : '<button class="lnkedit add" title="点一下补一个链接">＋链接</button>') + '</td>' +
    '<td class="c-topic"><button type="button" class="noteview" title="点一下改备注">' +
      (r["备注"] ? esc(r["备注"]) : '<span class="noteadd">＋备注</span>') + '</button></td>' +
    '<td class="c-op">' + mergeBtnHtml(comp) +
      '<button class="del" data-del="' + esc(r["id"]) + '" aria-label="删掉这条" title="删掉这条">×</button></td>';
  // 只有**代表一家公司**的行能拖（岗位子行没有 comp，拖它没有任何意义）。
  // 拖的是「这一行是哪家公司」，落点也是同样这些行 —— 见下面那段拖动并入。
  if(comp){
    tr.draggable = true;
    tr.dataset.drgKey = comp.key;
    tr.dataset.drgName = comp.name;
  }
  tr.querySelector(".st").onchange = e => setStatus(r["id"], e.target.value);
  tr.querySelector(".del").onclick = () => delApp(r["id"], r["公司名称"]);
  const nv = tr.querySelector(".noteview");
  if(nv) nv.onclick = () => editNote(nv, r);
  // 公司名 / 岗位名 / 投递时间：点一下就地改（跟备注一套行为）
  tr.querySelectorAll(".cellv[data-f]").forEach(el => {
    const c = APP_CELLS[el.dataset.f];
    if(!c) return;
    el.onclick = () => inlineEdit(el.closest("td"), {
      value: el.dataset.v, kind: c.kind, placeholder: c.ph,
      save: v => saveAppField(r["id"], c.api, v, c.label, el.dataset.v),
    });
  });
  const lk = tr.querySelector(".lnkedit");
  if(lk) lk.onclick = e => {
    e.stopPropagation();
    inlineEdit(lk.closest("td"), {value: r["网页链接"] || "", placeholder: "https://…",
      save: v => saveAppField(r["id"], "url", v, "链接", r["网页链接"] || "")});
  };
  return tr;
}

// 主行：这一组投了几个岗位、整体进度、最近一次投递、通知汇总；点一下展开 / 收起。
// 状态那一格显示的是「组里最靠后的状态」（真值在下面各个子行里，这里只做概要，
// 所以不做成可改的下拉 —— 一家公司有好几个状态，改哪个是不明确的）。
// lvl=1 是顶层（公司 / 主公司），lvl=2 是归并组里那家子公司（缩进 + 浅一点的箭头）。
// comp = 这一行代表哪家公司（主公司那行传的是它自己的名字 —— 纯容器型主行也能再往上并一层）；
// 岗位子行不传，所以没有按钮。
// 汇总那一行写什么：没归并 → 「N 个岗位」+ 岗位名；归并过 → 「N 家子公司 · N 个岗位」+ 子公司名。
function appNodeRow(n, open, lvl, comp){
  const env = n.env;
  const d = daysLeft(env.next);
  const over = isOverDl(env.next);
  const cls = env.next ? (over ? "over" : d === 0 ? "d0" : d <= 1 ? "d1" : d <= 3 ? "d3" : d <= 7 ? "d7" : "") : "";
  const dlTxt = !env.next ? "—" : over ? "已过期" : d === 0 ? "今天" : d + " 天";
  const since = daysSince(n.lastAt);
  const posed = n.rows.map(r => r["岗位名称"] || "（没写岗位）");
  const shorts = n.merged ? n.subs.map(c => c.name) : posed;
  const badge = n.merged ? (n.subs.length + " 家子公司 · " + n.rows.length + " 个岗位")
                         : (n.rows.length + " 个岗位");
  const tr = document.createElement("tr");
  tr.className = "grp" + (lvl === 2 ? " lvl2" : "") + (open ? " open" : "");
  // 岗位名 / 子公司名列表跟徽标一起放在**公司列内部**（第二行）：整组的宽度就被公司列框住了，
  // 主行不会把表格撑宽（.gpos 有 ellipsis）。各岗位的状态 / 备注在下面子行里，这里不重复。
  tr.innerHTML =
    '<td class="c-name">' +
      '<button class="gtoggle" aria-expanded="' + (open ? "true" : "false") +
        '" title="点这一行' + (open ? '收起' : '展开') +
        (n.merged ? '这家的子公司' : '这家的岗位') + '">' +
        '<span class="arw">▶</span><span>' + esc(n.label) + '</span></button>' +
      '<span class="gmeta"><span class="gn">' + badge + '</span>' +
        '<span class="gpos" title="' + esc(shorts.join(" / ")) + '">' + esc(shorts.join(" · ")) +
        '</span></span></td>' +
    '<td></td>' +
    '<td><span class="pillst st-' + esc(n.best) + '" title="这一组里最靠后的进度是「' + esc(n.best) +
      '」；每个岗位各自的状态展开后可以改">' + esc(n.best) + '</span></td>' +
    '<td class="dl">' + esc(n.lastAt || "—") +
      (since === null ? "" : '<small>最近 ' + since + ' 天前</small>') + '</td>' +
    '<td class="assoc">' + (env.count
        ? '<span class="yes">' + env.count + ' 条</span> · ' + env.types.map(t => esc(t)).join("/") +
          '<br><span class="dl ' + cls + '">最近截止 ' + dlTxt + '</span>'
        : '<span style="color:var(--faint)">暂无</span>') + '</td>' +
    '<td></td>' +
    '<td></td>' +
    '<td class="c-op">' + mergeBtnHtml(comp) + '</td>';
  // **整行都能点**。她的原话：「展开的方式也太不方便了，要去点这个小三角才能展开，
  // 需要点这一行就能展开」—— 小三角只有 10px，命中面积太小。
  // 绑在 tr 上、不绑在按钮上：按钮的 click 会冒泡过来，所以**不能再单独绑**，
  // 否则点三角会触发两次（等于没反应）。
  // INTER 是行内真正的交互元素（主行这几格基本都是空的，留着防以后加东西被误吞）。
  const INTER = "a,select,input,textarea,.noteview,.cellv,.lnkedit,.del,.mg";
  tr.onclick = e => {
    if(e.target && e.target.closest && e.target.closest(INTER)) return;
    if(Date.now() - dragEndAt < 300) return;   // 刚拖完那一下不算「点」（见下面那段拖动并入）
    // 点过的那家单独记下来：比全局默认优先，且点过一次就不再受「展开全部」的默认值摆布
    appOpenKeys[n.key] = !appOpenOf(n.key);
    renderApps();
  };
  // 主公司那行**也**能拖：主公司名打错了，直接把它拖到对的那家下面就行（不用先改挂再重来）
  if(comp){
    tr.draggable = true;
    tr.dataset.drgKey = comp.key;
    tr.dataset.drgName = comp.name;
  }
  return tr;
}
function renderApps(){
  // 正在拖一行的时候绝不重建表格 —— 后台每 10 秒会 pull 一次，一重建被拖的那行就从文档里
  // 换掉了，浏览器当场把这次拖动作废（表现是「拖到一半突然没了」）。
  if(dragChild) return;
  appSave();   // 筛选一变就记下来（渲染是这个页面唯一的收敛点，接在别处容易漏）
  const stChips = document.getElementById("appStatusChips");
  stChips.innerHTML = "";
  ST_ORDER.forEach(s => {
    const b = document.createElement("button");
    b.textContent = s;
    b.classList.toggle("on", appStatus === s);
    b.onclick = () => { appStatus = (appStatus === s) ? null : s; appQuick = "all"; renderApps(); };
    stChips.appendChild(b);
  });
  const any = appQuick !== "all" || appStatus || appQ;
  document.getElementById("appReset").hidden = !any;

  const list = APPS.filter(r => {
    if(appQuick !== "all" && appGroup(r) !== appQuick) return false;
    if(appStatus && r["状态"] !== appStatus) return false;
    if(appQ){
      const hay = ((r["公司名称"]||"")+(r["岗位名称"]||"")+(r["备注"]||"")).toLowerCase();
      if(!hay.includes(appQ)) return false;
    }
    return true;
  });
  const nodes = buildNodes(list);   // 组间按「最近一次投递」排好了；主公司 → 子公司 → 岗位的层也在里面

  const tb = document.getElementById("appTb");
  tb.innerHTML = "";
  // 有东西可折才算：「展开全部」管的是顶层那些行（子公司在主行摊开之后才轮到它）
  const folded = nodes.filter(n => n.merged || n.own.rows.length > 1).length;
  if(!list.length){
    tb.innerHTML = '<tr><td colspan="8" class="empty">还没有投递记录。装好扩展后去网申页投一次，点完提交就会自动弹出来；也可以点右上角「手动加一条」。</td></tr>';
  }
  // 搜索时自动摊开 —— 不然只剩一个名字，看不出命中的是哪个岗位
  const spread = !!appQ;
  nodes.forEach(n => {
    if(!n.merged){
      const g = n.own;
      if(g.rows.length === 1){ tb.appendChild(appRow(g.rows[0], false, "", g)); return; }
      const open = appOpenOf(g.key) || spread;
      tb.appendChild(appNodeRow(nodeOfComp(g), open, 1, g));
      if(open) g.rows.forEach(r => tb.appendChild(appRow(r, true, "", null)));
      return;
    }
    const open = appOpenOf(n.key) || spread;
    // 主公司那行**自己也要有按钮**：她要是把主公司名打错了（比如打成「示例建筑」），
    // 那一行没有按钮的话，十几家子公司就得一个个「拆出」再重并 —— 给它一个改挂的口子。
    // 顺带把「三层」也走通了（示例五局 → 示例建筑集团 → 示例建筑总公司，上溯那条链本来就是通的）。
    // ⚠️ key 传 **label 本身**（不是内部那个 "@xxx" 节点键）：落盘要干干净净的「示例建筑集团」，
    // 别让内部前缀混进 JSON；parentLabelOf 键和名都会试，照样找得到。
    tb.appendChild(appNodeRow(n, open, 1, n.own || {key: n.label, name: n.label}));
    if(!open) return;
    n.comps.forEach(c => {
      if(c === n.own){                     // 主公司自己投的那些：上面那行就是它，不重复写名字
        c.rows.forEach(r => tb.appendChild(appRow(r, true, "", null)));
        return;
      }
      // 只投了一个岗位的子公司：直接铺一条，别为一条还套一层壳（跟「一家只有一个岗位的单铺」一个道理）
      if(c.rows.length === 1){ tb.appendChild(appRow(c.rows[0], true, c.name, c)); return; }
      const o2 = appOpenOf(c.key);
      tb.appendChild(appNodeRow(nodeOfComp(c), o2, 2, c));
      if(o2) c.rows.forEach(r => tb.appendChild(appRow(r, true, "", null)));
    });
  });
  const oa = document.getElementById("appOpenAll");
  oa.hidden = !folded;
  oa.textContent = appOpenAll ? "收起全部" : "展开全部";
  // 无障碍 A4：全局展开开关也是展开点，状态要能让读屏说出来
  oa.setAttribute("aria-expanded", appOpenAll ? "true" : "false");

  // —— 计数一律按**行**（KPI / 分布 / 概览都是），跟折叠的口径一致 ——
  // 一家公司投几个岗位仍然只算一个「投递」；归并过的主公司也算一行（几家子公司在它下面）。
  // 这行的进度取组里最靠后的状态。
  // 注意：这几处用的是全量 allNodes，不跟着上面的筛选变 —— 卡片给的是全局盘子，
  // 点卡片才把下面的列表筛过去（点之前数字先动会让人以为"点了才算出这些"）。
  const allNodes = buildNodes(APPS);
  const nco = allNodes.length;
  const cnt = grp => allNodes.filter(n => n.group === grp).length;
  document.getElementById("appKpi").innerHTML =
    kpi(nco, "总投递", "t-total", "all", false, APPS.length + " 个岗位") +
    kpi(cnt("active"), "进行中", "t-active", "active", appQuick === "active") +
    kpi(cnt("interview"), "已约面", "t-interview", "interview", appQuick === "interview") +
    kpi(cnt("offer"), "Offer", "t-offer", "offer", appQuick === "offer") +
    kpi(cnt("closed"), "已结束", "t-closed", "closed", appQuick === "closed");
  document.querySelectorAll("#appKpi .kpi-card").forEach(el => {
    el.onclick = () => {
      const v = el.dataset.q;
      appQuick = (v === "all" || appQuick === v) ? "all" : v;
      appStatus = null;
      renderApps();
    };
  });

  const sc = {};
  allNodes.forEach(n => { sc[n.best] = (sc[n.best]||0) + 1; });
  bars(document.getElementById("appByStatus"),
       ST_ORDER.map(s => [s, sc[s] || 0, ST_COLOR[s] || "var(--faint)"]));

  const buckets = [["本周", 0, "var(--accent)"], ["上周", 0, "var(--blue)"], ["更早", 0, "var(--faint)"]];
  allNodes.forEach(n => {
    const d = daysSince(n.lastAt);
    if(d === null) return;
    if(d < 7) buckets[0][1]++; else if(d < 14) buckets[1][1]++; else buckets[2][1]++;
  });
  bars(document.getElementById("appByTime"), buckets);

  const nApps = APPS.length;
  // 归并前后两个口径都报：「涉及公司」是源表的原始家数，KPI / 徽标上的「家」是归并后剩几行。
  // 只报一个必然有人觉得对不上（她自己一行行数是按行数的）。
  const rawCo = buildGroups(APPS).length;
  const mergedN = mergedSubs(allNodes);
  const withNotice = allNodes.filter(n => (n.env || {}).count).length;
  const last = allNodes.slice().sort((a,b) => (ts(b.lastAt)||0) - (ts(a.lastAt)||0))[0];
  const sinceList = allNodes.map(n => daysSince(n.lastAt)).filter(x => x !== null);
  const avg = sinceList.length ? Math.round(sinceList.reduce((s,x) => s+x, 0) / sinceList.length * 10) / 10 : 0;
  // —— 近 7 天投递趋势：数据在这儿算好，画法交给 trendChart ——
  // 今天排在最右边。日期一律拿「本地的年月日」对齐（跟 ts / daysSince 的读法一致，
  // 用 toISOString 那种 UTC 切法在东八区会把凌晨投的算到前一天去）。
  const t0 = (() => { const n = new Date(); return Date.UTC(n.getFullYear(), n.getMonth(), n.getDate()); })();
  const tDays = [];
  for(let i = 6; i >= 0; i--){
    const t = t0 - i * 86400000, d = new Date(t);
    tDays.push({
      t: t, n: 0, cos: [],
      label: String(d.getUTCMonth() + 1).padStart(2, "0") + "-" + String(d.getUTCDate()).padStart(2, "0"),
      dow: "日一二三四五六"[d.getUTCDay()],
    });
  }
  let tRest = 0;
  allNodes.forEach(n => {
    const t = ts(n.lastAt);
    if(!isNaN(t)){
      const d = new Date(t);
      const slot = tDays.find(x => x.t === Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
      if(slot){ slot.n++; slot.cos.push(n.label); return; }
    }
    tRest++;
  });
  trendChart(document.getElementById("appTrend"), tDays);

  // 图底下只留两行（她 2026-10-10：「趋势图大一点，不要下面的文字，
  // 可以留最近一次投递和近 7 天投递数量」）。
  // ⚠️ **别的数一个都没扔** —— 全挂成这一块的 `title`（鼠标停上去照旧看得见），
  // 包括原来写在图下面那句「另有 N 家不在这一周内」。别嫌它长，这是不占版面的代价。
  const tTot = tDays.reduce((s, d) => s + d.n, 0);
  const ovEl = document.getElementById("appOverview");
  ovEl.innerHTML =
    kv("近 7 天投递数量", tTot + " 家") +
    kv("最近一次投递", last ? (last.label + " · " + String(last.lastAt).slice(5, 10)) : "—");
  ovEl.title = [
    "涉及公司 " + rawCo + " 家" + (mergedN ? "（归并后 " + nco + " 行）" : ""),
    "投递岗位 " + nApps + " 个",
    "平均已投天数 " + avg + " 天",
    "已收到通知的公司 " + withNotice + " / " + nco + " 家",
    mergedN ? "已归并子公司 " + mergedN + " 家" : "",
    tRest ? "另有 " + tRest + " 家不在这一周内（更早投的，或者还没填投递时间）" : "",
  ].filter(Boolean).join("；");
  document.getElementById("appCount").textContent = any
    ? "筛出 " + nodes.length + " 家 · " + list.length + " 个岗位"
    : "共 " + nco + " 家 · " + nApps + " 个岗位";
  document.getElementById("tabApps").textContent = nco;
  document.getElementById("tabNotices").textContent = NOTICES.length;
  // 离线样例 / 没配飞书表：去飞书同步这条路走不通，按钮别摆着（点了必然失败）
  const sb = document.getElementById("appSync");
  if(sb) sb.hidden = OFFLINE || (META.apps_source !== "feishu");
  const si = document.getElementById("appSyncInfo");
  if(si){
    if(OFFLINE) si.textContent = "离线样例，读的是导出那一刻的快照。";
    else if(META.apps_source !== "feishu") si.textContent = "没连飞书表，只看本地记录。";
    else if(META.apps_bad) si.textContent = "上次同步没成功（" + (META.apps_state || "")
      + "），下面这份是本地记录，过几分钟会自己再试。";
    else si.textContent = "每 " + (META.apps_ttl_min || 30) + " 分钟自动同步一次"
      + (META.apps_sync_at ? "，上次 " + META.apps_sync_at : "")
      + "。你在飞书表里把某条改成「待一面」这种，这边过一会儿会自己变。";
  }
}
function kpi(n, label, tone, key, on, sub){
  const z = Number(n) === 0 ? " zero" : "";
  return '<button class="kpi-card ' + tone + z + (on ? " on" : "") + '" data-q="' + key + '">' +
         '<b>' + n + '</b><span>' + label + '</span>' +
         (sub ? '<em>' + esc(sub) + '</em>' : '') + '</button>';
}
async function setStatus(id, value){
  const r = await fetch("/api/status", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id: id, status: value})});
  const d = await r.json().catch(() => ({}));
  if(d.ok){ say("状态已改成「" + value + "」"); pull(); } else { say("改失败了：" + (d.error || r.status)); }
}
async function delApp(id, name){
  if(!confirm("删掉「" + name + "」这条记录？删了不影响邮箱那边的通知。")) return;
  const r = await fetch("/api/delete", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id: id})});
  const d = await r.json().catch(() => ({}));
  if(d.ok){ say("已删除"); pull(); } else { say("删失败了：" + (d.error || r.status)); }
}
// 备注：点一下就地变成输入框。Enter 存 / Esc 撤 / 点到别处也算存。
// 和「状态」共用 /api/status，只是传的字段不同（note 而不是 status）。
// v0.23.1 起**直接委托 inlineEdit()** —— 备注列只有 72px，原来那套「清空格子 + 塞一个
// `min-width:130px` 的输入框」会把整张表撑到 1131px（总宽 +81）并让 22 行文字重新换行。
// 现在跟公司名 / 岗位名一样是浮框，表格纹丝不动。
// 2026-10-08 无障碍 A1：入参从 span 改叫 el —— .noteview 现在是 <button>，不是 span 了，
// 名字再叫 span 就是下一个「结论改了、名字没跟」的坑。行为没变。
function editNote(el, r){
  inlineEdit(el.closest("td"), {
    value: r["备注"] || "",
    placeholder: "内推码 / 面试时间 / HR 说了啥…",
    save: v => saveNote(r["id"], v, r["备注"] || ""),
  });
}
async function saveNote(id, value, old){
  if(value === old){ renderApps(); return; }   // 没改就不发请求
  const r = await fetch("/api/status", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({id: id, note: value})});
  const d = await r.json().catch(() => ({}));
  if(d.ok){ say(value ? "备注已保存" : "备注清空了"); pull(); }
  else { say("备注没存上：" + (d.error || r.status)); renderApps(); }
}

// 表格里「点一下就地改」的通用件 —— 公司名 / 岗位名 / 投递时间 / 链接都走它。
// 行为跟备注那套对齐：Enter 存 / Esc 撤 / 点到别处也算存。
// 投递时间用原生日期时间选择器（`datetime-local`），**空值预填今天** ——
// 她 2026-10-05 提的「改时间方式能不能方便一点，还要我一个个手打。。」：
// 那格原来是空的就退化成纯文本框（只显示「＋时间」），只能从零敲一个日期。
// 认不出的老值（「10 月初」这种）仍旧退回普通输入框 —— 宁可她手打，也别把值吃掉。
function dayStr(off){          // 0=今天 -1=昨天 -2=前天（本地时区，跟后端 now_str 一个口径）
  const d = new Date();
  d.setDate(d.getDate() + (off || 0));
  const p = n => (n < 10 ? "0" : "") + n;
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
}
let cellDlSeq = 0;   // 每开一次「并入」面板就换个 datalist id（同一个 id 不许在文档里出现两次）
function inlineEdit(td, opts){
  const old = String(opts.value == null ? "" : opts.value);
  let cancelled = false;
  const pop = document.createElement("div");
  pop.className = "cellpop";
  const inp = document.createElement("input");
  inp.className = "cellin";
  // 空值也上日期选择器（老值认得出来才用；认不出的自由文本照旧走 text）
  const isTime = opts.kind === "when" && (!old || /^\d{4}-\d{2}-\d{2}/.test(old));
  if(isTime){
    pop.classList.add("has-quick");
    inp.type = "datetime-local";
    if(old){
      // datetime-local 只认 `2026-10-04T13:34`；老记录里只写日期的（`2026-09-14`）补上 00:00，
      // 否则浏览器会把非法值**静默清空**，她一打开编辑框就发现原来的时间没了
      let v = old.replace(" ", "T");
      if(v.length === 10) v += "T00:00";
      inp.value = v.slice(0, 16);
    }else{
      inp.value = dayStr(0) + "T00:00";   // 空值预填今天：打开就有值，不用再从零敲
    }
  }else{
    inp.type = "text";
    inp.value = old;
    if(opts.placeholder) inp.placeholder = opts.placeholder;
  }
  pop.appendChild(inp);
  // 「并入」那格：挂一份公司名候选（她表里现有的公司），也能直接手打一个新名字 ——
  // 比如把三家「示例X」并到「示例建筑集团」下面，而这个主公司她表里本来没有，得能自己写。
  if(opts.kind === "merge"){
    const dl = document.createElement("datalist");
    dl.id = "coNames" + (++cellDlSeq);
    (opts.options || []).forEach(v => {
      const o = document.createElement("option");
      o.value = v;
      dl.appendChild(o);
    });
    pop.appendChild(dl);
    inp.setAttribute("list", dl.id);
    if(old) pop.classList.add("has-quick");   // 有「不并了」那颗按钮，输入框收窄一点
  }
  if(isTime){
    // 「今天 / 昨天 / 前天」：点一下**直接存**（不用先点日历选日期再回车）。
    // 原值带钟点的（`2026-09-29 15:41` 这种）保留那个钟点，别一键抹成 00:00 ——
    // 她记录里绝大多数是 00:00，但真有非零的，抹平就是改坏她的数据。
    const hm = (/^\d{4}-\d{2}-\d{2} (\d{2}:\d{2})$/.exec(old) || [])[1] || "";
    const keep = (hm && hm !== "00:00") ? hm : "00:00";
    const q = document.createElement("div");
    q.className = "cellquick";
    [["今天", 0], ["昨天", -1], ["前天", -2]].forEach(pair => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = pair[0];
      b.title = "投递时间设为" + pair[0] + " " + keep;
      b.dataset.off = pair[1];
      q.appendChild(b);
    });
    q.querySelectorAll("button").forEach(b => {
      b.onclick = ev => {
        ev.stopPropagation();
        ev.preventDefault();
        cancelled = true;              // 别让 blur 那条路再写一次（点按钮时 input 会先失焦）
        opts.save(dayStr(Number(b.dataset.off)) + " " + keep);
      };
    });
    pop.appendChild(q);
  }
  // 「并入」那格：已经并过的给一颗「不并了」——点一下直接拆回来，不用把框里的字删干净。
  // （框里清空 = 什么都不改，免得手滑点走就把归并抹了；要取消得按这颗。）
  if(opts.kind === "merge" && old){
    const q = document.createElement("div");
    q.className = "cellquick";
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "不并了";
    b.title = "取消归并 —— 这家单独占一行（内置打底的那条也一起盖掉）";
    b.onclick = ev => {
      ev.stopPropagation();
      ev.preventDefault();
      cancelled = true;
      (opts.clear || opts.save)("");
    };
    q.appendChild(b);
    pop.appendChild(q);
  }
  // 别清空格子：原内容留着、只是隐身（visibility:hidden），尺寸就照旧占住，
  // 表格布局一个像素都不动；浮层绝对定位盖在上面，想给多宽就多宽。
  const box = td.closest("table").getBoundingClientRect();
  const me = td.getBoundingClientRect();
  td.classList.add("editing");
  td.appendChild(pop);
  // 浮层比格子宽得多（时间那格还有三个按钮），放不下就贴右边缘往左铺 ——
  // 早先按「格子中点是否在表格右半」判断，那是给固定 240px 定的；浮层宽度现在会变，
  // 直接拿真实宽度比，才不会有的格子顶出去、有的白白靠右。
  const pw = pop.getBoundingClientRect().width;
  if(me.left + pw > box.right + 2) td.classList.add("anchor-r");
  // 兜底：表格自己横向滚动时（窗口拖到 700 多 px 才出现），靠右对齐也可能顶出**视口** ——
  // 实测 740px 会顶出去 9px，肉眼看就是右边那个按钮缺一角。用 transform 整体推回来
  // （不用 margin：`right:0` 定位下 margin-left 会掺进 shrink-to-fit 的计算）。
  const vOver = pop.getBoundingClientRect().right - (window.innerWidth - 8);
  if(vOver > 0) pop.style.transform = "translateY(-50%) translateX(" + (-vOver) + "px)";
  inp.focus();
  try{ inp.select(); }catch(e){ /* 日期类输入框不支持 select，不是错 */ }
  inp.onkeydown = ev => {
    if(ev.key === "Enter"){ ev.preventDefault(); inp.blur(); }
    else if(ev.key === "Escape"){ ev.preventDefault(); cancelled = true; renderApps(); }
  };
  inp.onblur = () => {
    if(cancelled) return;
    let v = inp.value.trim();
    if(inp.type === "datetime-local") v = v ? v.replace("T", " ").slice(0, 16) : "";
    if(v === old){ renderApps(); return; }    // 没改就不发请求
    opts.save(v);
  };
}

// 改一格 → 写 /api/status（参数名见后端 APP_EDIT_FIELDS 白名单）
async function saveAppField(id, api, value, label, old){
  if(value === String(old == null ? "" : old)){ renderApps(); return; }
  const body = {id: id};
  body[api] = value;
  try{
    const r = await fetch("/api/status", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)});
    const d = await r.json().catch(() => ({}));
    if(d.ok){ say(label + "已保存"); pull(); }
    else { say(label + "没存上：" + (d.error || r.status)); renderApps(); }
  }catch(e){ say("连不上本地服务：" + e.message); renderApps(); }
}

// 「并入」面板：点「并入 / 拆出」→ 就地出来一个框，填主公司名（候选是她表里现有的公司名，
// 也能直接打一个新的 —— 比如「示例建筑集团」她表里本来没有）。Enter 存 / Esc 撤 / 点到别处也算存。
// **框里清空 = 什么都不改**：要取消归并得点旁边的「不并了」，免得她点走就把归并抹了。
function editMerge(btn){
  if(OFFLINE){ say("这是离线样例，改不了 —— 打开本地服务再改"); return; }
  const child = btn.dataset.mg || "", name = btn.dataset.mgname || child;
  const old = btn.dataset.mgparent || "";
  const names = [...new Set(APPS.map(r => String(r["公司名称"] == null ? "" : r["公司名称"]).trim())
    .filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh"));
  inlineEdit(btn.closest("td"), {
    value: old, kind: "merge", options: names,
    placeholder: "主公司名，比如 示例建筑集团",
    save: v => { if(!v){ renderApps(); return; } saveAppParent(child, name, v, old); },
    clear: () => saveAppParent(child, name, "", old),
  });
}
// 写一条「并入」→ /api/app-parent。写完直接用手上这份新表重画，不用等下一次轮询。
async function saveAppParent(child, name, parent, old){
  if(parent === old){ renderApps(); return; }          // 没改就不发请求
  try{
    const r = await fetch("/api/app-parent", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({child: child, parent: parent})});
    const d = await r.json().catch(() => ({}));
    if(d.ok){
      APP_PARENT = d.app_parents || {};
      say(parent ? ("「" + name + "」已并入「" + parent + "」") : ("「" + name + "」不再归并"));
      renderApps();
    }else{ say("没存上：" + (d.error || r.status)); renderApps(); }
  }catch(e){ say("连不上本地服务：" + e.message); renderApps(); }
}
document.getElementById("appQ").oninput = e => { appQ = e.target.value.trim().toLowerCase(); renderApps(); };
document.getElementById("appReset").onclick = () => {
  appQuick = "all"; appStatus = null; appQ = ""; document.getElementById("appQ").value = ""; renderApps();
};
// 一键展开 / 收起所有多岗位的公司。清掉单家的覆盖，免得跟默认值互相打架。
document.getElementById("appOpenAll").onclick = () => {
  appOpenAll = !appOpenAll;
  appOpenKeys = {};
  renderApps();
};
// 「并入 / 拆出」：**一次委托**，别在每次渲染里给按钮单独绑一遍
// （这张表踩过「绑两次 = 看着像没反应」的坑）。stopPropagation 免得顺带把整行展开。
document.getElementById("appTb").addEventListener("click", e => {
  const b = e.target && e.target.closest ? e.target.closest("button.mg") : null;
  if(!b) return;
  e.stopPropagation();
  e.preventDefault();
  editMerge(b);
});

// ---------------- 拖动并入 ----------------
// 她 2026-10-07 第二句：「并入方法太麻烦了，能不能拖动并入」。
// 点按钮那条路要开面板、还得把主公司名打一遍（「示例建筑集团」这种她表里本来没有的名字只能手打），
// 十一家子公司就是十一次手打。所以：**把一家公司那一行拖到另一家公司行上，松手就并过去**。
//   拖的 = 任意「代表一家公司」的行（顶层公司 / 主公司 / 子公司 / 只投了一个岗位的公司）
//   落点 = 同样这些行；松手 → 这一家并到「落点那一行代表的那家公司」下面
// 原来的「并入」按钮**保留**：① 第一次建主公司（表里还没有这个名字）得靠它；
// ② 拆出来、改挂到别的名字也还靠它。拖动省掉的是「同一个名字反复填」那一半。
// ⚠️ 四件必须做的事：
//   ① `dragover` 不 preventDefault，浏览器根本不让 drop（表现为「怎么拖都没反应」）；
//   ② 拖到自己身上、或拖到自己的子孙下面会成环 —— 拦住并说句话，别默默并出一个环；
//   ③ 从输入框 / 下拉 / 按钮 / 链接上按下的不算拖这一行，否则她编辑到一半整行会被拖走；
//   ④ 拖完那一下的 click 要吞掉，不然会顺带把落点那行展开 / 收起（见 appNodeRow 里的判断）。
// ⚠️ 这三个状态用 `var` 而不是 `let`：`appNodeRow` 里要读 `dragEndAt`，而 `renderAll()` 可能
//    在这段代码**后面**才第一次跑 —— `let` 会撞 TDZ 直接报错；`var` 提升成 undefined
//    （`Date.now() - undefined` 是 NaN，判定 false = 不抑制），正好是想要的默认行为。
var dragChild = null, dragLast = null, dragEndAt = 0;
// 拖动时不许重建表格：10 秒一次的轮询会替换掉被拖的那一行，浏览器就把这次拖动作废了
const DRAG_NO = "input,textarea,select,.cellpop,.cellin,button,a";
function dragRowOf(t){ return (t && t.closest) ? t.closest("tr[draggable='true']") : null; }
// 沿 startLabel 的主公司链往上走，看有没有经过 who（有 → 再并过去就成环了）
function chainHas(startLabel, who){
  let cur = startLabel, guard = 0;
  while(cur && guard++ < 8){
    if(coNorm(cur) === coNorm(who)) return true;
    cur = parentLabelOf(PKEY + coNorm(cur), cur);
  }
  return false;
}
function dragClear(){
  document.getElementById("appTb")
    .querySelectorAll("tr.dragging,tr.dropok")
    .forEach(t => t.classList.remove("dragging", "dropok"));
  dragLast = null;
}
// 拖完把主公司那条链上的行都摊开 —— 不然她刚拖完只看到被拖那行不见了，主公司在哪儿得自己找
function openAlongChain(name){
  let p = name, guard = 0;
  while(p && guard++ < 8){
    appOpenKeys[PKEY + coNorm(p)] = true;   // 它当「主公司」时的节点键（renderApps 判断展开用的是这个）
    appOpenKeys[coNorm(p)] = true;          // 它自己当子公司行时用的键（第二层那个）
    p = parentLabelOf(PKEY + coNorm(p), p);
  }
}
// 松手：把 c 这一家并到 tr 那一行代表的公司下面
function dropMerge(c, tr){
  const parent = tr.dataset.drgName || "";
  const old = parentLabelOf(c.key, c.name);
  if(!parent || parent === c.name){ say("要拖到另一家公司那一行上才行"); return; }
  if(parent === old){ say("「" + c.name + "」本来就在「" + old + "」下面"); return; }
  if(chainHas(parent, c.name)){
    say("不能并到「" + parent + "」下面 —— 那边已经（或马上要）挂在「" + c.name + "」上了");
    return;
  }
  openAlongChain(parent);      // 先把展开状态记下，saveAppParent 里那次 renderApps 就会照它铺开
  saveAppParent(c.key, c.name, parent, old);
}
const appTbEl = document.getElementById("appTb");
appTbEl.addEventListener("dragstart", e => {
  const tr = dragRowOf(e.target);
  if(!tr || (e.target.closest && e.target.closest(DRAG_NO))){ e.preventDefault(); return; }
  dragChild = {key: tr.dataset.drgKey, name: tr.dataset.drgName, row: tr};
  dragEndAt = 0;
  try{ e.dataTransfer.setData("text/plain", dragChild.key); }catch(_){}   // 不设的话有些浏览器压根不开始拖
  if(e.dataTransfer) e.dataTransfer.effectAllowed = "link";
  tr.classList.add("dragging");
  say("拖到另一家公司那一行上，松手就并过去");
});
appTbEl.addEventListener("dragover", e => {
  if(!dragChild) return;
  const tr = dragRowOf(e.target);
  if(!tr || tr === dragChild.row){          // 自己身上 / 空白处：不放行（光标会显示「不能放」）
    if(dragLast){ dragLast.classList.remove("dropok"); dragLast = null; }
    return;
  }
  e.preventDefault();                        // ⚠️ 少了这一句，drop 永远不会触发
  if(e.dataTransfer) e.dataTransfer.dropEffect = "link";
  if(dragLast !== tr){                       // 换目标时把上一行的高亮收掉
    if(dragLast) dragLast.classList.remove("dropok");
    tr.classList.add("dropok");
    dragLast = tr;
  }
});
appTbEl.addEventListener("dragleave", e => {
  const tr = dragRowOf(e.target);
  if(tr && tr === dragLast && !(e.relatedTarget && tr.contains(e.relatedTarget))){
    tr.classList.remove("dropok"); dragLast = null;
  }
});
appTbEl.addEventListener("drop", e => {
  e.preventDefault();                        // 不拦的话浏览器会拿 text/plain 去搜别的
  const tr = dragRowOf(e.target), c = dragChild;
  dragClear(); dragChild = null; dragEndAt = Date.now();
  if(c && tr && tr !== c.row) dropMerge(c, tr);
});
appTbEl.addEventListener("dragend", () => {
  dragClear(); dragChild = null; dragEndAt = Date.now();
});
const addForm = document.getElementById("addForm");
document.getElementById("addToggle").onclick = () => {
  addForm.hidden = !addForm.hidden;
  if(!addForm.hidden) document.getElementById("fCompany").focus();
};
document.getElementById("fCancel").onclick = () => { addForm.hidden = true; };
document.getElementById("fStatus").innerHTML = ST_ORDER.map(s => '<option>' + s + '</option>').join("");
document.getElementById("fSave").onclick = async () => {
  const body = {
    company: document.getElementById("fCompany").value.trim(),
    position: document.getElementById("fPosition").value.trim(),
    url: document.getElementById("fUrl").value.trim(),
    status: document.getElementById("fStatus").value,
    note: document.getElementById("fNote").value.trim(),
    source: "手动",
  };
  if(!body.company){ say("公司名称得填一个"); return; }
  const r = await fetch("/api/apply", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  const d = await r.json().catch(() => ({}));
  if(d.ok){
    say(d.duplicate ? "这条已经记过了" : "记好了");
    ["fCompany","fPosition","fUrl","fNote"].forEach(i => { document.getElementById(i).value = ""; });
    addForm.hidden = true;
    pull();
  } else { say("存失败：" + (d.error || r.status)); }
};
document.getElementById("refreshBtn").onclick = () => { pull(true); };
// 「同步飞书进度」：先让后端去拉，页面这边每 1.2 秒问一次好了没（一条页 2~5 秒）
document.getElementById("appSync").onclick = async () => {
  const b = document.getElementById("appSync");
  if(b.disabled) return;
  const before = META.apps_sync_at || "";
  b.disabled = true; b.textContent = "同步中…";
  try{
    const r = await fetch("/api/apps-sync", {method:"POST", headers:{"Content-Type":"application/json"},
                                            body:"{}", cache:"no-store"});
    const d0 = await r.json().catch(() => ({}));
    if(d0 && d0.ok === false){ say("同步不了：" + (d0.error || r.status)); }
    else {
      for(let i = 0; i < 12; i++){
        await new Promise(res => setTimeout(res, 1200));
        await pull(false);
        if(!META.apps_busy && ((META.apps_sync_at || "") !== before || i >= 2)) break;
      }
      say(META.apps_bad ? ("同步失败：" + (META.apps_state || "")) : (META.apps_state || "已同步"));
    }
  }catch(e){ say("同步请求发不出去：" + e.message); }
  b.disabled = false; b.textContent = "同步飞书进度";
};

// ---------------- 邮箱通知 ----------------
const NT_STATES = META.notice_states || ["待处理","已完成","已过期","已忽略"];
const NT_CLOSED = META.notice_closed || ["已完成","已过期","已忽略"];
const ntSt = r => r["处理状态"] || "待处理";
const ntDone = r => NT_CLOSED.indexOf(ntSt(r)) >= 0;

let ntType = null, ntQ = "", ntQuick = "all", ntSort = "urg", ntHideDone = false;
// 「按公司归纳」（她 2026-10-08 原话：「统计到面板时候同样公司的邮件归纳到一起，可展开」）。
// 默认开 —— 她要的就是这个形态；关掉回到一行一封（两种形态都保留，旧视图不删）。
let ntGrouped = true;
// 展开状态记内存（按 _gkey），翻页 / 轮询重渲染都不丢；刷新页面回到全收起
const NT_GRP_OPEN = {};

// —— 邮箱通知的筛选记忆（搜索词 / 类型 / 快筛 / 排序 / 隐藏已处理）——
const NT_QKEY = "at.notices.q.v1";
// v0.22.0：顶部那三张「今天截止 / 3 天内 / 逾期」的卡砍了，对应的快筛值也不再作为入口。
// 老存档里若还躺着这些值，读的时候直接丢掉（留着只会让她打开页面看见一张被筛过、
// 却找不到是哪个按钮在生效的表）。
const NT_QUICKS = ["all", "todo", "done", "urgent"];
const NT_SORTS = ["urg", "dl", "co"];
let ntQLoaded = false;
function ntSave(){
  if(!ntQLoaded) return;
  lsSave(NT_QKEY, {q: ntQ, type: ntType, quick: ntQuick, sort: ntSort, hideDone: ntHideDone,
                   grouped: ntGrouped});
}
function ntLoad(){
  const d = lsLoad(NT_QKEY);
  ntQLoaded = true;
  if(!d) return;
  if(typeof d.q === "string") ntQ = d.q;
  if(NT_QUICKS.indexOf(d.quick) >= 0) ntQuick = d.quick;
  if(NT_SORTS.indexOf(d.sort) >= 0) ntSort = d.sort;
  if(typeof d.hideDone === "boolean") ntHideDone = d.hideDone;
  // 归纳开关她自己切过就以她为准；没切过（老存档没这个键）保持默认开
  if(typeof d.grouped === "boolean") ntGrouped = d.grouped;
  // 通知类型是从数据里现取的：存档里那个类型要是已经不存在了，留着只会筛出空表，直接丢掉
  if(typeof d.type === "string" &&
     NOTICES.some(r => (r["通知类型"] || []).indexOf(d.type) >= 0)) ntType = d.type;
  const el = document.getElementById("ntQ");
  if(el) el.value = ntQ;
}
ntLoad();   // 必须排在第一次 renderNotices() 之前


// 分组顺序 = 紧急程度顺序：过期没办的都在最上面
const NT_GROUPS = [
  ["over",    "已逾期未处理", "var(--red)"],
  ["today",   "今天截止",          "var(--red)"],
  ["d3",      "3 天内截止",        "var(--orange)"],
  ["d7",      "这一周内",          "var(--blue)"],
  ["later",   "一周以上",          "var(--muted)"],
  ["unknown", "没识别出截止", "var(--faint)"],
  // ⚠️ 2026-10-09 新增：她连着两次报「你又给我漏邮件了」，两次都落在这条缝里
  // （一封是当天 21:00 截止的笔试通知、一封是次日 10:21 才截止的测评通知）—— 她自己标了「已完成」，
  // 于是那两行既不计入「还没处理」，又整行灰字沉到 40 家里最难看见的位置，
  // 而它们的截止时间**根本还没到**。「已完成」这个标记对她更像「我看过了 / 我记下了」，
  // 不等于「时间上这件事已经结束」。所以把「标了已完成、但截止还没到」单独拎成一段，
  // 排在灰区（已经处理掉的）之前，照常报剩余时间。判据见 ntOpenDone()。
  ["check",   "标了已完成、但截止还没到", "var(--orange)"],
  ["done",    "已经处理掉的",       "var(--green)"]
];
const NT_ORDER = NT_GROUPS.map(g => g[0]);
// v0.22.0：原来这里还有 NT_PANEL（「截止紧迫度」面板的四档）和 NT_PANEL_REST，
// 那两块面板连同它们的取数口径一起砍了 —— 表格里的 7 档分组照旧走 NT_GROUPS。
// 「已完成」要分两种看（2026-10-09 补）：
//   ① 截止**已经过去**的 → 真了结，沉底、距截止画破折号（她 2026-10-03 定的，照旧）；
//   ② 截止**还没到**的 → 她标了已完成，但时间上这件事还没结束，必须照样看得见。
// 判据只认「处理状态 ∈ 已了结」+「截止时间存在且还没过」，不去猜她标得对不对。
function ntOpenDone(r){
  if(!ntDone(r)) return false;
  const d = daysLeft(r["通知截止时间"]);
  return d !== null && !isOverDl(r["通知截止时间"]);
}
function ntGroup(r){
  if(ntDone(r)) return ntOpenDone(r) ? "check" : "done";
  const d = daysLeft(r["通知截止时间"]);
  if(d === null) return "unknown";
  if(isOverDl(r["通知截止时间"])) return "over";
  if(d === 0) return "today";
  if(d <= 3) return "d3";
  if(d <= 7) return "d7";
  return "later";
}
function ntQuickMatch(r){
  if(ntQuick === "all") return true;
  if(ntQuick === "todo") return !ntDone(r);
  if(ntQuick === "done") return ntDone(r);
  const g = ntGroup(r);
  if(ntQuick === "soon") return g === "today" || g === "d3";
  if(ntQuick === "urgent") return g === "over" || g === "today" || g === "d3";
  return g === ntQuick;
}
// 「现在到底筛着什么」—— 空表处与「清除筛选」按钮共用同一份口径，不许两处各写一套。
// 2026-10-09：她报「你又给我漏邮件了」，真因是类型标签**持久化**（localStorage）而选中态不显眼，
// 她早先点过的「资料补充」一直挂着，搜某家公司名必然是 0 条（那条是「测评」）。
function ntConds(){
  const out = [];
  if(ntQ) out.push("搜「" + ntQ + "」");
  if(ntType) out.push("类型「" + ntType + "」");
  if(ntQuick === "todo") out.push("只看没处理的");
  else if(ntQuick === "done") out.push("只看已处理的");
  else if(ntQuick === "urgent") out.push("只看紧急的");
  else if(ntQuick === "soon") out.push("只看 3 天内截止的");
  if(ntHideDone) out.push("隐藏已处理");
  return out;
}
function ntClearFilters(){
  ntQuick = "all"; ntType = null; ntQ = ""; ntHideDone = false;
  const q = document.getElementById("ntQ");
  if(q) q.value = "";
  renderNotices();
}
function ntCmp(a, b){
  const co = String(a["公司名称"] || "").localeCompare(String(b["公司名称"] || ""), "zh");
  // v0.22.0：进度条搬去第二个视图之后，这张表回到纯粹的「按截止时间 / 紧急程度」排序 ——
  // 「有进度的排最前」那条规则跟着进度条一起挪走了（两个地方都算进度只会互相打架）。
  if(ntSort === "co") return co;
  // 已处理的一律沉底，免得陈年旧账占着最上面
  const aDone = ntDone(a), bDone = ntDone(b);
  if(aDone !== bDone) return aDone ? 1 : -1;
  if(ntSort === "urg"){
    const ga = NT_ORDER.indexOf(ntGroup(a)), gb = NT_ORDER.indexOf(ntGroup(b));
    if(ga !== gb) return ga - gb;
  }
  const da = daysLeft(a["通知截止时间"]), db = daysLeft(b["通知截止时间"]);
  if(da === null && db !== null) return 1;
  if(db === null && da !== null) return -1;
  if(da !== null && db !== null && da !== db) return da - db;
  return co;
}
function ntStateSelect(r){
  const st = ntSt(r);
  const opts = NT_STATES.indexOf(st) >= 0 ? NT_STATES : [st].concat(NT_STATES);
  const local = r["状态来源"] === "本地";
  const tip = local ? "你手动标的，飞书同步不会覆盖它；选「清除我的标记」可以还原"
            : (r["状态来源"] === "飞书" ? "来自飞书表，可以在这里改" : "还没标，在这里标一下");
  // 本地改过的，给一个回到飞书原值的出口
  const undo = local ? '<option value="">（清除我的标记）</option>' : "";
  return '<select class="sts s-' + esc(st) + (local ? " local" : "") + '" title="' + esc(tip) + '">' +
    undo + opts.map(s => '<option value="' + esc(s) + '"' + (s === st ? " selected" : "") + '>' + esc(s) + '</option>').join("") +
    '</select>';
}
async function setNoticeState(key, value, sel){
  if(!key){ say("这条没有稳定标识，改不了"); if(sel) sel.disabled = false; return; }
  try{
    const r = await fetch("/api/notice-state", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({key: key, status: value})});
    const d = await r.json().catch(() => ({}));
    if(d.ok){
      say(value ? "已标成「" + value + "」" : "已清除手动标记");
      pull();
    }else { say("改失败了：" + (d.error || r.status)); if(sel) sel.disabled = false; }
  }catch(e){ say("连不上本地服务：" + e.message); if(sel) sel.disabled = false; }
}

// ---- 通知类型那一格：点一下就能选（2026-10-08 她提的）----
// 类型拿不准的邮件不再丢去单独的 CSV，是通知表里正常的一行，**类型留空**；
// 「待确认」这三个字是这里渲染时加的角标，不写进数据（UI 文案别混进 CSV）。
// 一条标了多个类型的（飞书那边本来就写着「测评、笔试」两个）保持原来的彩色标签，不改成下拉 ——
// 一个下拉只能选一个值，会把另一个类型吞掉。
function ntTypeCell(r){
  const ts = r["通知类型"] || [];
  const tip = r["依据"] ? ' title="' + esc(r["依据"]) + '"' : "";
  // 拒信 / Offer 这两个类型是引擎自己认的（不在手选名单里）——
  // 单类型也渲染成标签，别塞进下拉：下拉里没这个选项，会显示成「待确认」骗人。
  if(ts.length > 1 || (ts[0] && (META.mail_types || []).indexOf(ts[0]) < 0)){
    return ts.map(t =>
      '<span class="tag"' + tip +
      ' style="color:' + (TYPE_COLOR[t] || "var(--muted)") + '">' + esc(t) + '</span>').join("");
  }
  const cur = ts[0] || "";
  return '<select class="nty"' + tip +
    (cur ? ' style="color:' + (TYPE_COLOR[cur] || "var(--muted)") + '"' : "") + '>' +
    '<option value=""' + (cur ? "" : " selected") + '>待确认</option>' +
    (META.mail_types || []).map(t =>
      '<option value="' + esc(t) + '"' + (t === cur ? " selected" : "") + '>' + esc(t) + '</option>').join("") +
    '</select>';
}
async function setNoticeType(key, value, sel){
  if(!key){ say("这条没有稳定标识，改不了"); if(sel) sel.disabled = false; return; }
  try{
    const r = await fetch("/api/notice-type", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({key: key, ntype: value})});
    const d = await r.json().catch(() => ({}));
    if(d.ok){
      say(value ? "已标成「" + value + "」" : "已改回「待确认」");
      // 必须重拉：类型进了通知 key 的哈希，改完 key 就变了，手上的旧数据已经对不上
      pull();
    }else { say("改失败了：" + (d.error || r.status)); if(sel) sel.disabled = false; }
  }catch(e){ say("连不上本地服务：" + e.message); if(sel) sel.disabled = false; }
}

// ---- 手动补一条通知（v0.35.0，她的原话：「邮箱通知那个看板增加一个手动添加的功能」）----
// 有些面试 / 测评通知是短信或电话来的，邮箱里没有。手动行只存本机 data/notices_manual.json，
// 不回写飞书 —— 它跟邮件行在同一个表里排序、一起算截止。
// 她的铁律：能不给她的地方就别让她手打；格子空着也要预填合理默认值（截止预填「明天 23:59」），
// 唯独「说明」留空不预填 —— 那是「只有她知道的事」，猜错反而误导。
function ntPad2(n){ return String(n).padStart(2, "0"); }
function ntToday(days, endOfDay){
  const d = new Date();
  d.setDate(d.getDate() + days);
  if(endOfDay) d.setHours(23, 59, 0, 0);
  return d.getFullYear() + "-" + ntPad2(d.getMonth() + 1) + "-" + ntPad2(d.getDate()) +
    "T" + ntPad2(d.getHours()) + ":" + ntPad2(d.getMinutes());
}
function ntCompanyCands(){
  const s = new Set();
  (NOTICES || []).forEach(r => { const c = (r["公司名称"] || "").trim(); if(c) s.add(c); });
  (APPS || []).forEach(r => { const c = (r["公司名称"] || "").trim(); if(c) s.add(c); });
  return [...s].sort((a, b) => a.localeCompare(b, "zh"));
}
function ntBuildAddBox(){
  const types = ['<option value="">（待确认）</option>'].concat(
    (META.mail_types || []).map(t =>
      '<option value="' + esc(t) + '"' + (t === "面试" ? " selected" : "") + '>' + esc(t) + '</option>'));
  const cands = ntCompanyCands().map(c => '<option value="' + esc(c) + '"></option>').join("");
  const box = document.getElementById("ntAddBox");
  box.innerHTML =
    '<div class="addrow"><label for="ntAddCo">公司名称</label>' +
      '<input type="text" id="ntAddCo" list="ntCoList" placeholder="必填">' +
      '<datalist id="ntCoList">' + cands + '</datalist></div>' +
    '<div class="addrow"><label for="ntAddPos">岗位名称</label>' +
      '<input type="text" id="ntAddPos" placeholder="可留空"></div>' +
    '<div class="addrow"><label for="ntAddType">类型</label>' +
      '<select id="ntAddType">' + types.join("") + '</select></div>' +
    '<div class="addrow"><label for="ntAddDl">截止时间</label>' +
      '<span class="dlrow"><input type="datetime-local" id="ntAddDl" value="' + ntToday(1, true) + '">' +
        '<button type="button" class="quick" data-d="0">今天 23:59</button>' +
        '<button type="button" class="quick" data-d="1">明天 23:59</button>' +
        '<button type="button" class="quick" data-d="2">后天 23:59</button></span></div>' +
    '<div class="addrow"><label for="ntAddLink">链接</label>' +
      '<input type="text" id="ntAddLink" placeholder="可留空，没带 https:// 会补上"></div>' +
    '<div class="addrow"><label for="ntAddNote">说明</label>' +
      '<input type="text" id="ntAddNote" placeholder="只有你知道的事"></div>' +
    '<div class="addrow act"><button type="button" id="ntAddSave">保存</button>' +
      '<button type="button" id="ntAddCancel">取消</button></div>';
  box.querySelectorAll("button.quick").forEach(b => {
    b.onclick = () => { document.getElementById("ntAddDl").value = ntToday(Number(b.dataset.d), true); };
  });
  document.getElementById("ntAddSave").onclick = saveManualNotice;
  document.getElementById("ntAddCancel").onclick = closeNtAdd;
}
function openNtAdd(){
  const box = document.getElementById("ntAddBox");
  ntBuildAddBox();               // 每次打开按最新数据重建（类型 / 公司候选都跟后端一致）
  box.hidden = false;
  const co = document.getElementById("ntAddCo");
  if(co) co.focus();
}
function closeNtAdd(){
  const box = document.getElementById("ntAddBox");
  box.hidden = true;
  box.innerHTML = "";            // 取消 = 关面板 + 清空输入
}
async function saveManualNotice(){
  const co = document.getElementById("ntAddCo").value.trim();
  if(!co){ say("先填公司名"); return; }     // 公司名空着**不发请求**
  const payload = {
    公司名称: co,
    岗位名称: document.getElementById("ntAddPos").value.trim(),
    类型: document.getElementById("ntAddType").value,
    // datetime-local 是 "2026-10-12T23:59"，后端只收空格分隔 —— 前端负责换
    截止时间: document.getElementById("ntAddDl").value.trim().replace("T", " "),
    链接: document.getElementById("ntAddLink").value.trim(),
    说明: document.getElementById("ntAddNote").value.trim(),
  };
  try{
    const r = await fetch("/api/notice-add", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)});
    const d = await r.json().catch(() => ({}));
    if(d.ok){ say("已添加「" + co + "」"); closeNtAdd(); pull(); }
    else { say(d.error || ("添加失败：" + r.status)); }   // 失败面板不关，输入留着
  }catch(e){ say("连不上本地服务：" + e.message); }
}
async function delManualNotice(mid, name){
  if(!mid){ say("这条没有标识，删不了"); return; }
  try{
    const r = await fetch("/api/notice-del", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({id: mid})});
    const d = await r.json().catch(() => ({}));
    if(d.ok){ say("已删掉「" + (name || "这条") + "」"); pull(); }
    else { say("删失败了：" + (d.error || r.status)); }
  }catch(e){ say("连不上本地服务：" + e.message); }
}
document.getElementById("ntAdd").onclick = () => {
  const box = document.getElementById("ntAddBox");
  if(box.hidden) openNtAdd(); else closeNtAdd();
};
// 点别处就把「确认删」恢复成「删」（点在按钮本身上时不动，交给它的 onclick）。
// 用 click 而不是 keydown —— 无障碍 A1 钉死过「不许加全局键盘监听」。
document.addEventListener("click", e => {
  const t = e.target;
  if(t && t.closest && t.closest("button.ntdel")) return;
  document.querySelectorAll("button.ntdel.confirm").forEach(b => {
    clearTimeout(b._t); b.classList.remove("confirm"); b.textContent = "删";
  });
}, true);

// ---- 第二个视图：投递进度（v0.22.0 从「表格里的一列」搬回来的）----
// 一家公司一行，画这家走到哪一轮。她定的两条：
//   ① 只停在「测评」阶段（还没约到面试）的公司**整条不列** —— 这个视图是拿来看面试的，
//      一堆「还在测评」的混在里面等于没有重点；
//   ② 止步的公司照样留着（她自己说过：留个记录，免得每次看都在想「这家到底挂了没」）。
const IV_DROP = /^测评/;
function ivShown(it){
  // 挂了的照样列出来 —— 这个视图存在的意义就是「这家到底挂了没，一眼能看见」，
  // 所以止步的不受「只停在测评就不列」那条约束（2026-10-05 她提的：示例涂料公司挂了要看得见）。
  if(it["止步"]) return true;
  const cur = it["当前"] || "";
  if(!cur) return false;          // 连走到哪都没识别出来，别硬摆上去
  return !IV_DROP.test(cur);
}
// 走得最远的排最前（offer / 终面 先看见），同档按公司名；止步的一律沉到最后
function ivShowList(){
  const rank = it => {
    const nodes = it["节点"] || [];
    const i = nodes.indexOf(it["当前"] || "");
    return i < 0 ? -1 : i;
  };
  return IVS.filter(ivShown).sort((a, b) => {
    const sa = a["止步"] ? 1 : 0, sb = b["止步"] ? 1 : 0;
    if(sa !== sb) return sa - sb;
    const ra = rank(a), rb = rank(b);
    if(ra !== rb) return rb - ra;
    return String(a["公司"] || "").localeCompare(String(b["公司"] || ""), "zh");
  });
}

// ---- 邮箱直连的提示条（v0.29.0）----
// ① 没配邮箱：给「怎么配」，不报错、可跳过（接不接邮箱是用户的选择）；
// ② 配了但有认不出公司的邮件：给一句待办 + 一键归类。归类写 data/mail_aliases.json
//    （发件人域名 → 公司名），下次同一发件人发来的就自动认了 —— 越用越准，无大模型（ARCH §4.2）。
// 数据源的人话名 —— 状态行里要说清「这张表现在读的是什么」
function noticesSrcName(){
  const m = META.notices_mode;
  return m === "feishu" ? "飞书表"
       : m === "file" ? "本地 CSV"
       : m === "imap" ? "邮箱直连"
       : "没接数据源";
}
// 邮箱连接状态那一行（有可用凭据时才调）—— 她 2026-10-08 的硬要求：改了什么必须看得见。
//   imap        = 真在用的直连（谁 / 多久收一次 / 上次结果 / 收一次 / 断开）
//   feishu/file = 连上了但没在用（备用直连）—— **这一行就是「连完没反应」那个 bug 的正面修复**：
//                 她连完邮箱，飞书模式下也必须看得见「已连上」，并能一键改成直连。
function mailStatusHTML(){
  const who = esc(META.mail_user || META.mail_host || "");
  if(META.notices_mode === "imap"){
    const extra = META.mail_unknown_n
      ? ('　·　' + META.mail_unknown_n + ' 封没认出类型，已在下表，标着「待确认」') : "";
    return '已连接 <b>' + who + '</b>' +
      '　·　每 ' + (META.mail_ttl_min || 15) + ' 分钟自动收信　·　' +
      esc(META.sync_msg || "") + extra +
      '　<button class="link" id="mailSyncBtn">收一次邮件</button>' +
      '　<button class="link" id="mailOffBtn">断开</button>';
  }
  // 「连上了但没在用」必须把**哪个邮箱**写出来 —— 她连完要能一眼确认「是我那个邮箱」，
  // 只写「邮箱」两个字等于没说（2026-10-08 她连完没反应，就是要找这句话）。
  return '已连上 <b>' + who + '</b>（备用直连，没在用）　·　这张表当前读的是' + noticesSrcName() +
    '　<button class="link" id="mailUseBtn">改用直连收信</button>' +
    '　<button class="link" id="mailOffBtn">断开</button>';
}
// 状态行上的按钮接线。状态行每次重铺后节点都是新的，必须重新接一遍。
function mailStatusWire(){
  const sb = document.getElementById("mailSyncBtn");
  if(sb) sb.onclick = () => {
    sb.disabled = true;
    fetch("/api/mail-sync", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})
      .then(r => r.json()).then(d => say(d.msg || "已开始收邮件…"))
      .catch(e => say("连不上本地服务：" + e.message))
      .finally(() => { setTimeout(() => { sb.disabled = false; }, 4000); });
  };
  const ub = document.getElementById("mailUseBtn");
  if(ub) ub.onclick = () => {          // 「改用直连收信」→ 把这张表的数据源切到邮箱直连
    ub.disabled = true;
    fetch("/api/mail-source", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({source: "imap"})})
      .then(r => r.json()).then(d => {
        if(d.ok === false){ say(d.error || "切不过去"); ub.disabled = false; return; }
        say(d.msg || "已改用邮箱直连…");
        setTimeout(() => pull(false), 800);
      })
      .catch(e => { say("连不上本地服务：" + e.message); ub.disabled = false; });
  };
  const ob = document.getElementById("mailOffBtn");
  if(ob) ob.onclick = () => {          // 「断开」→ 关掉直连（授权码留着，下次点回来还在）
    ob.disabled = true;
    fetch("/api/mail-off", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})
      .then(r => r.json()).then(d => {
        if(d.ok === false){ say(d.error || "断开失败"); ob.disabled = false; return; }
        say(d.msg || "已断开邮箱直连");
        setTimeout(() => pull(false), 800);
      })
      .catch(e => { say("连不上本地服务：" + e.message); ob.disabled = false; });
  };
}
function mailBar(){
  const el = document.getElementById("ntMailBar");
  if(!el) return;
  const un = DATA.mail_unmatched || [];
  const hasMail = !!(META.mail_configured && META.mail_enabled);   // 有可用凭据
  if(META.notices_mode === "imap" && un.length){
    el.hidden = false;
    el.className = "mailbar todo";
    let h = '<b>' + un.length + ' 封邮件没认出公司</b>' +
            '（先没进通知表，免得挂错公司）。给它指定一家公司，下次这个发件人发来的就自动认了：';
    un.forEach((r, i) => {
      h += '<div class="mrow">' +
        '<span title="' + esc(r["邮件主题"] || "") + '" style="flex:1 1 300px;min-width:0;' +
        'overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
        esc(r["邮件主题"] || "（无主题）") + '</span>' +
        '<code style="color:var(--faint)">' + esc(r["发件人域名"] || "") + '</code>' +
        '<input type="text" data-ix="' + i + '" placeholder="公司名（用你名单里的写法）">' +
        '<button class="link" data-ix="' + i + '">就这家</button></div>';
    });
    // 待办下面照样带一行连接状态 —— 归类归到一半也不该看不见「邮箱连没连上」
    h += '<div class="mrow">' + mailStatusHTML() + '</div>';
    el.innerHTML = h;
    el.querySelectorAll("button[data-ix]").forEach(b => {
      b.onclick = () => {
        const i = +b.dataset.ix, r = un[i];
        const inp = el.querySelector('input[data-ix="' + i + '"]');
        const co = ((inp && inp.value) || "").trim();
        if(!co){ say("先在框里填个公司名"); return; }
        mailAlias(r["发件人域名"] || "", co, b);
      };
    });
    mailStatusWire();
    return;
  }
  // ② 有可用凭据：**任何模式**都显示邮箱连接状态（她 2026-10-08：改了什么必须看得见）。
  //    原 bug 就是 feishu 模式下 el.hidden=true —— 她连完邮箱，这一页一个字都不变。
  if(hasMail){
    el.hidden = false;
    el.className = "mailbar";
    el.innerHTML = mailStatusHTML();
    mailStatusWire();
    return;
  }
  // ②b 凭据还在、只是被「断开」掉了：给一键连回来 —— 授权码本来就躺在 config.ini 里，
  //     再让她去 QQ 邮箱重新生成一遍是纯折磨（她定的规矩：能不给她的地方就别让她手打）。
  if(META.mail_configured){
    el.hidden = false;
    el.className = "mailbar";
    el.innerHTML = '邮箱直连已断开（<b>' + esc(META.mail_user || "") + '</b> 的授权码还留着）' +
      '　<button class="link" id="mailOnBtn">重新连上</button>' +
      '　<button class="link" id="mailSetupBtn">换一个邮箱</button>';
    const on2 = document.getElementById("mailOnBtn");
    if(on2) on2.onclick = () => {
      on2.disabled = true;
      fetch("/api/mail-on", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})
        .then(r => r.json()).then(d => {
          if(d.ok === false){ say(d.error || "连不回来"); on2.disabled = false; return; }
          say("邮箱直连已重新连上");
          setTimeout(() => pull(false), 800);
        })
        .catch(e => { say("连不上本地服务：" + e.message); on2.disabled = false; });
    };
    const sb4 = document.getElementById("mailSetupBtn");
    if(sb4) sb4.onclick = openMailSetup;
    return;
  }
  // ③ 没凭据：
  if(META.notices_mode === "off"){
    el.hidden = false;
    el.className = "mailbar";
    // 她 2026-10-08：「让直连邮箱简单点，小白也能连」—— 入口从终端命令换成页面按钮，
    // 点开弹卡填邮箱 + 授权码就行。终端向导降级为备选，文字里留一句。
    el.innerHTML = '<b>邮箱通知还没接。</b>接上以后，看板自己会去你的邮箱收测评 / 笔试 / AI面 / 面试 / 资料补充通知，' +
      '不用再手动导表（数据只在本机，不往外发）。' +
      '<br><button class="link" id="mailSetupBtn">连接我的邮箱</button>（不用开终端；' +
      '习惯命令行的话也可以跑 <code>python3 applytracker.py --mail-config</code>）。' +
      '<br>不想接也没关系，投递进度、机会库这些照常用。';
    const sb2 = document.getElementById("mailSetupBtn");
    if(sb2) sb2.onclick = openMailSetup;
    return;
  }
  // 已经在用飞书表 / 本地 CSV，但没连邮箱：给一行紧凑入口 ——
  // 「连接我的邮箱」不再只在 off 模式出现（小白也能连）。
  el.hidden = false;
  el.className = "mailbar";
  el.innerHTML = '没连邮箱（当前这张表读' + noticesSrcName() + '，够用）　' +
    '<button class="link" id="mailSetupBtn">连接我的邮箱</button>';
  const sb3 = document.getElementById("mailSetupBtn");
  if(sb3) sb3.onclick = openMailSetup;
}

function mailAlias(domain, company, btn){
  if(btn) btn.disabled = true;
  fetch("/api/mail-alias", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({domain: domain, company: company})})
    .then(r => r.json())
    .then(d => {
      if(d.ok){
        say("记下了：" + domain + " → " + company + "（下次同发件人自动认）");
        setTimeout(() => pull(false), 800);   // 后台重扫一轮，稍后这一页就自己更新
      }else{
        say(d.error || "写失败了");
        if(btn) btn.disabled = false;
      }
    })
    .catch(e => { say("连不上本地服务：" + e.message); if(btn) btn.disabled = false; });
}

function renderNotices(){
  mailBar();
  ntSave();   // 筛选一变就记下来
  document.getElementById("ntUrgent").classList.toggle("on", ntQuick === "urgent");
  document.getElementById("ntSortUrg").classList.toggle("on", ntSort === "urg");
  document.getElementById("ntSortDl").classList.toggle("on", ntSort === "dl");
  document.getElementById("ntSortCo").classList.toggle("on", ntSort === "co");
  document.getElementById("ntHideDone").classList.toggle("on", ntHideDone);
  document.getElementById("ntOnlyTodo").classList.toggle("on", ntQuick === "todo");
  document.getElementById("ntGrouped").classList.toggle("on", ntGrouped);

  const chips = document.getElementById("ntTypeChips");
  const types = [...new Set(NOTICES.flatMap(r => r["通知类型"] || []))]
    .sort((a,b) => (TYPE_ORDER.indexOf(a) + 99) % 99 - (TYPE_ORDER.indexOf(b) + 99) % 99);
  chips.innerHTML = "";
  types.forEach(t => {
    const b = document.createElement("button");
    b.textContent = t;
    b.classList.toggle("on", ntType === t);
    b.onclick = () => { ntType = (ntType === t) ? null : t; renderNotices(); };
    chips.appendChild(b);
  });

  const appOf = n => APPS.filter(a => sameCompanyTight(a["公司名称"], n["公司名称"]))[0];
  const n = NOTICES.length;
  const open = NOTICES.filter(r => !ntDone(r));
  const doneN = n - open.length;
  // 「已处理、但截止还没到」的条数 —— 单独报一个数（2026-10-09）。它是「已处理」这个数里的
  // 一小撮，但恰恰是最容易让她错过的那撮（当天 21:00 截止的笔试、次日 10:21 截止的测评）。
  const checkN = NOTICES.filter(r => ntOpenDone(r)).length;

  const list = NOTICES.filter(r => {
    if(!ntQuickMatch(r)) return false;
    if(ntHideDone && ntDone(r)) return false;
    if(ntType && !(r["通知类型"]||[]).includes(ntType)) return false;
    if(ntQ){
      const hay = ((r["公司名称"]||"")+(r["岗位名称"]||"")+(r["邮件主题"]||"")).toLowerCase();
      if(!hay.includes(ntQ)) return false;
    }
    return true;
  }).sort(ntCmp);

  const tb = document.getElementById("ntTb");
  tb.innerHTML = "";
  if(!list.length){
    // 「筛出 0」时只说「试试清除筛选」是不够的 —— 她 2026-10-09 就是被这个坑到：
    // 类型标签是**记住**的（localStorage），她之前点过「资料补充」，搜某家公司名当场 0 条，
    // 而那条恰恰是「测评」。她看到空表 → 打开 QQ 邮箱自证 → 「你又给我漏邮件了」。
    // 所以：把生效条件一条条念出来，再给一颗就地清除的按钮。
    const _cds = ntConds();
    tb.innerHTML = '<tr><td colspan="9" class="empty">' +
      (META.notices_ok
        ? (!n ? "还没有通知数据。"
           : (_cds.length
             ? "当前筛着 " + esc(_cds.join(" · ")) + " —— 这个条件组合下一条都没有。<br>" +
               "去掉筛选就能看到全部 " + n + " 条。" +
               ' <button type="button" id="ntClearEmpty">清除全部筛选</button>'
             : "没有符合条件的通知（共 " + n + " 条）。"))
        : (META.sync_bad ? "飞书同步失败：" + esc(META.sync_msg || "")
          : "还没接上邮箱数据：点上面的「连接我的邮箱」，或跑一次 python3 applytracker.py --mail-config。")) +
      '</td></tr>';
    const _ce = document.getElementById("ntClearEmpty");
    if(_ce) _ce.onclick = () => { ntClearFilters(); };
  }
  // —— 「同公司归纳」的归堆（按 _gkey 精确同组，后端派生；旧缓存没有 _gkey 时按公司名兜底）——
  // 组的顺序跟当前排序走：每组取第一条（list 已经按 ntCmp 排过）当代表，代表们的先后就是组的先后。
  // 筛选 / 搜索 / 快筛全部作用在**明细**上：某公司筛完 0 条 → 不进 groups → 整组自然消失；
  // 公司行上的「N 条」就是筛后条数。
  const gmap = {}, groups = [];
  list.forEach(r => {
    const k = r["_gkey"] || (r["公司名称"] || "").trim() || "（未知公司）";
    if(!gmap[k]){ gmap[k] = []; groups.push(k); }
    gmap[k].push(r);
  });

  // 一条明细行（两种形态共用）。归纳形态下明细行**始终渲染**、折叠时只挂 .ntfold 藏起来
  // （display:none）：一是展开/收起不用重排数据，二是隐藏的行还在 DOM 里，
  // 状态下拉的值照常能被读 —— 轮询重渲染也不会把没展开的组弄丢。
  const detailRow = (r, sub, fold) => {
    const g = ntGroup(r), done = ntDone(r), chk = ntOpenDone(r);
    const d = daysLeft(r["通知截止时间"]), over = isOverDl(r["通知截止时间"]);
    // 处理掉的不再报「过期 N 天」（她 2026-10-03 提的）：那件事已经做完了，
    // 留着红字只会在表里制造噪音。只画一个破折号。
    // ⚠️ 唯一的例外是「标了已完成、但截止还没到」（chk，见 ntOpenDone）：那条**照常报剩余时间**、
    // 整行**也不灰** —— 灰掉正是她两次看不见它的直接原因（2026-10-09 那两封笔试 / 测评通知）。
    const cls = (done && !chk) ? "" : (d === null ? "" : (over || d === 0) ? "d0" : d <= 3 ? "d3" : d <= 7 ? "d7" : "");
    const txt = (done && !chk) ? "—"
      : chk ? (d === 0 ? "今天截止" : "还有 " + d + " 天")
      : d === null ? "未识别"
      : over ? (d < 0 ? "过期 " + (-d) + " 天" : "今天截止")
      : d === 0 ? "今天截止" : d + " 天";
    const a = appOf(r);
    const tr = document.createElement("tr");
    tr.className = ((done && !chk) ? "done " : "") + "g-" + g + (sub ? " ntsub" : "") + (fold ? " ntfold" : "");
    tr.innerHTML =
      '<td class="c-name">' + esc(r["公司名称"]) +
        (r["来源"] === "手动"
          ? ' <span class="tag" style="color:var(--orange)">手动</span>' +
            '<button class="ntdel" type="button" aria-label="删掉这条手动通知" title="删掉这条手动通知">删</button>'
          : "") +
        '</td>' +
      '<td class="c-pos">' + esc(r["岗位名称"] || "—") + '</td>' +
      '<td class="pill">' + ntTypeCell(r) + '</td>' +
      '<td class="dl">' + esc(r["通知截止时间"] || "—") + '</td>' +
      '<td class="dl ' + cls + ((done && !chk) ? " dl-off" : "") + '">' + txt + '</td>' +
      '<td>' + ntStateSelect(r) + '</td>' +
      '<td>' + (a ? '<span class="pillst st-' + esc(a["状态"]) + '">' + esc(a["状态"]) + '</span>'
                   : '<span style="color:var(--faint)">没投过</span>') + '</td>' +
      '<td>' + (r["邮件链接"] ? '<a href="' + esc(r["邮件链接"]) + '" target="_blank" rel="noopener">打开</a>' : "—") + '</td>' +
      '<td class="c-topic">' + esc(r["邮件主题"] || "") + '</td>';
    tb.appendChild(tr);
    const sel = tr.querySelector("select.sts");
    if(sel){
      sel.onchange = () => { sel.disabled = true; setNoticeState(r["_key"], sel.value, sel); };
    }
    const tsel = tr.querySelector("select.nty");
    if(tsel){
      tsel.onchange = () => { tsel.disabled = true; setNoticeType(r["_key"], tsel.value, tsel); };
    }
    // 手动行的删除入口：点一下变「确认删」，再点一下才真发 —— 免得手滑一下就没了。
    // 点别处（下面那个全局 click 兜底）或等 4 秒就恢复。
    const dbtn = tr.querySelector("button.ntdel");
    if(dbtn){
      dbtn.onclick = () => {
        if(!dbtn.classList.contains("confirm")){
          dbtn.classList.add("confirm");
          dbtn.textContent = "确认删";
          dbtn._t = setTimeout(() => { dbtn.classList.remove("confirm"); dbtn.textContent = "删"; }, 4000);
          return;
        }
        clearTimeout(dbtn._t);
        dbtn.disabled = true;
        delManualNotice(r["_mid"], r["公司名称"]);
      };
    }
  };

  if(ntGrouped){
    // —— 归纳形态（她拍板的默认）：同公司一条公司行，点开看明细 ——
    // 展开入口是行内原生 <button> + aria-expanded（无障碍规矩照旧：行上不许 tabindex，
    // 键盘用户走按钮，Enter 天然触发；整行可点只是鼠标的补充入口）。
    // ⚠️ 归纳形态原来**一条分段标题都没有** —— 40 家公司平铺下来，「已完成」的那十几行
    // 只靠灰字区分，往下滚就是一片灰，看不出边界。2026-10-09 她报「你又给我漏邮件了」
    // 时，那条正好沉在第 37/40 位。补一条「已经处理掉的」分隔条：只**加**一行，不动任何数据、
    // 不把任何东西藏起来（她 2026-10-03、10-04 两次否掉的都是「藏起来/淡下去」）。
    // ⚠️ 渲染顺序 = 三堆拼起来：普通（未了结）→「标了已完成、但截止还没到」→「已经处理掉的」。
    // 不能只靠 groups 自带的排序「遇到第一家 check 就插段头」（v0.33.6 第一版就是这么写的，当场踩了）：
    // 一家**同时**有「待处理」和「已完成未到期」的公司（她表里就有这么一家）按排序会落在纯 check 公司前面，
    // 段头就被插到它头上，紧跟其后的「没识别出截止」那两家眼睛一看就掉进
    // 「标了已完成、但截止还没到」标题底下 —— 标题说的和底下列的是两回事。
    // 所以先按「这家有没有 check 行」把组分成三堆、再按堆渲染：段头一插，底下的行就一定属于它。
    // 段的两个口径（家数 / 条数）也跟着堆算，跟堆里的行数天然对得上。
    const chkSoon = k => {
      const ds = gmap[k].filter(r => ntOpenDone(r))
        .map(r => daysLeft(r["通知截止时间"])).filter(x => x !== null);
      return ds.length ? Math.min.apply(null, ds) : 9999;
    };
    const checkKeys = groups.filter(k => gmap[k].some(r => ntOpenDone(r)))
      .sort((a, b) => chkSoon(a) - chkSoon(b));
    const isChk = k => checkKeys.indexOf(k) >= 0;
    const doneKeys = groups.filter(k => !isChk(k) && gmap[k].every(r => ntDone(r)));
    const isDn = k => doneKeys.indexOf(k) >= 0;
    const order = groups.filter(k => !isChk(k) && !isDn(k)).concat(checkKeys, doneKeys);
    let doneSectShown = false, checkSectShown = false;
    order.forEach(k => {
      const rows = gmap[k];
      // ⚠️ 「还没真正了结的」有两类：没标已完成的，**和**标了已完成但截止还没到的（ntOpenDone）。
      // 后者必须算进来 —— 不然那行的组头既灰又没截止时间，正是她两次看不见它的样子。
      const openRows = rows.filter(r => !ntDone(r) || ntOpenDone(r));
      const allDone = openRows.length === 0;
      const isCheck = rows.some(r => ntOpenDone(r));
      // 「标了已完成、但截止还没到」单独拎一段，排在灰区之前（2026-10-09 她连着两次报「漏」）
      if(isCheck && !checkSectShown){
        checkSectShown = true;
        // 两个口径都从**要渲染的那一堆**数，不另外算一套（段头说 4 家，底下的行就得正好是那 4 家）
        const cGrp = checkKeys.length;
        const cCnt = checkKeys.reduce((s, x) => s + gmap[x].filter(r => ntOpenDone(r)).length, 0);
        const trc = document.createElement("tr");
        trc.className = "sect g-check";
        trc.innerHTML = '<td colspan="9"><div class="sectr">' +
          '<span class="dot" style="background:var(--orange)"></span>标了已完成、但截止还没到' +
          '<span style="color:var(--muted);font-weight:500">（时间还没到，别当成已经结束）</span>' +
          '<span class="sn">' + cGrp + ' 家 · ' + cCnt + ' 条</span></div></td>';
        tb.appendChild(trc);
      }
      if(allDone && !doneSectShown){
        doneSectShown = true;
        const dGrp = doneKeys.length;
        const dCnt = list.filter(r => ntDone(r) && !ntOpenDone(r)).length;
        const trd = document.createElement("tr");
        trd.className = "sect g-done";
        trd.innerHTML = '<td colspan="9"><div class="sectr">' +
          '<span class="dot" style="background:var(--green)"></span>已经处理掉的' +
          '<span style="color:var(--muted);font-weight:500">（' +
          NT_CLOSED.map(esc).join(" / ") + '）</span>' +
          '<span class="sn">' + dGrp + ' 家 · ' + dCnt + ' 条</span></div></td>';
        tb.appendChild(trd);
      }
      // 最早的一个**未完成**截止：已处理的不算 —— 那件事做完了，别顶着组头上
      const soonest = openRows.filter(r => daysLeft(r["通知截止时间"]) !== null)
        .sort((a,b) => daysLeft(a["通知截止时间"]) - daysLeft(b["通知截止时间"]))[0];
      const gtypes = [...new Set(rows.flatMap(r => r["通知类型"] || []))]
        .sort((a,b) => (TYPE_ORDER.indexOf(a) + 99) % 99 - (TYPE_ORDER.indexOf(b) + 99) % 99);
      const isOpen = !!NT_GRP_OPEN[k];
      const sd = soonest ? daysLeft(soonest["通知截止时间"]) : null;
      const sOver = soonest ? isOverDl(soonest["通知截止时间"]) : false;
      const sCls = sd === null ? "" : (sOver || sd === 0) ? "d0" : sd <= 3 ? "d3" : sd <= 7 ? "d7" : "";
      const sTxt = sd === null ? "" : sOver ? (sd < 0 ? "过期 " + (-sd) + " 天" : "今天截止")
        : sd === 0 ? "今天截止" : sd + " 天";
      const trg = document.createElement("tr");
      // 公司行是「单格 flex 组头」：样式全在自己身上（.ntco），不借 sect 的类 ——
      // 紧急度分段行（.sect）只在平铺形态出现，两种行的探测口径要分得开
      trg.className = "ntco" + (allDone ? " done" : "");
      trg.innerHTML =
        '<td colspan="9"><div class="ntcor">' +
        '<button class="ntexp" aria-expanded="' + (isOpen ? "true" : "false") + '"' +
        ' title="' + (isOpen ? "收起 " : "展开 ") + esc(k) + ' 的 ' + rows.length + ' 条通知">' +
        '<span class="arw">▸</span>' + esc(k) + '</button>' +
        '<span class="ntn">' + rows.length + ' 条</span>' +
        gtypes.map(t => '<span class="tag" style="color:' +
          (TYPE_COLOR[t] || "var(--muted)") + '">' + esc(t) + '</span>').join("") +
        (soonest ? '<span class="ntdl dl ' + sCls + '">' + esc(soonest["通知截止时间"] || "") +
          (sTxt ? " · " + sTxt : "") + '</span>' : "") +
        '</div></td>';
      const tog = () => { NT_GRP_OPEN[k] = !NT_GRP_OPEN[k]; renderNotices(); };
      trg.querySelector(".ntexp").onclick = tog;
      // 整行也能点（点按钮的地方让按钮自己处理，别双触发）
      trg.onclick = (e) => { if(e.target.closest("button")) return; tog(); };
      tb.appendChild(trg);
      rows.forEach(r => detailRow(r, true, !isOpen));
    });
  }else{
    // —— 平铺形态（原来的一行一封，保留不删）——
    let curGroup = null;
    list.forEach(r => {
      const g = ntGroup(r);
      // 按紧急程度分段：已逾期 / 今天 / 3 天内 …；换成「按截止时间 / 按公司」就不分段
      // （那两种排序本来就不分段，硬插标题只会把顺序搅乱）。
      // 归纳形态下不分段 —— 分段会把公司组搅乱，组内自己的先后已经跟排序走了。
      if(ntSort === "urg" && g !== curGroup){
        curGroup = g;
        const meta = NT_GROUPS.filter(x => x[0] === g)[0] || ["", g, "var(--faint)"];
        const cnt = list.filter(x => ntGroup(x) === g).length;
        const tr0 = document.createElement("tr");
        tr0.className = "sect g-" + g;
        tr0.innerHTML = '<td colspan="9"><div class="sectr">' +
          '<span class="dot" style="background:' + meta[2] + '"></span>' +
          esc(meta[1]) + '<span class="sn">' + cnt + ' 条</span></div></td>';
        tb.appendChild(tr0);
      }
      detailRow(r, false);
    });
  }

  // ---- KPI：v0.22.0 砍到三张（她嫌顶部乱、重复）----
  // 原来「今天截止 / 3 天内截止 / 逾期未处理」各占一张卡，三个数还经常都是 0；
  // 现在并进「还没处理」那张的副标里。想只看紧要的，下面那排有「只看紧急的」按钮。
  const overOpen = open.filter(r => ntGroup(r) === "over").length;
  const todayOpen = open.filter(r => ntGroup(r) === "today").length;
  const d3Open = open.filter(r => ntGroup(r) === "d3").length;
  const pctDone = n ? Math.round(doneN / n * 100) : 0;
  const openSub = !open.length ? "全处理完了"
    : (overOpen || todayOpen || d3Open)
      ? "今天 " + todayOpen + " · 3 天内 " + (todayOpen + d3Open) + " · 逾期 " + overOpen
      : "都在一周以外";
  document.getElementById("ntKpi").innerHTML =
    kpi(n, "总通知数", "t-total", "all", false, doneN + " 条已处理") +
    kpi(open.length, "还没处理", "t-active", "todo", ntQuick === "todo", openSub) +
    kpi(doneN, "已处理", "t-done", "done", ntQuick === "done",
        pctDone + "% 完成" + (checkN ? " · 其中 " + checkN + " 条截止还没到" : ""));
  document.querySelectorAll("#ntKpi .kpi-card").forEach(el => {
    el.onclick = () => {
      const q = el.dataset.q;
      ntQuick = (ntQuick === q) ? "all" : q;
      renderNotices();
    };
  });

  // ---- 视图切换条右边那句话 ----
  // 原来顶部还有一条「待办：N 条…」的提示条和一行 secttl 小标题，说的都是同一件事
  // （几个数 + 一句说明），她嫌乱，v0.22.0 一起砍了，并成这一行小字。
  const hintEl = document.getElementById("ntvHint");
  if(hintEl) hintEl.textContent = (ntGrouped ? groups.length + " 家公司 · " : "") +
    "共 " + n + " 条通知 · " + open.length + " 条还没处理" +
    // 「已处理」里的这一小撮必须在最显眼的那句话里出现 —— 她两次都是从这句话下面开始找表的
    (checkN ? " · " + checkN + " 条标了已完成但截止还没到" : "");

  // ---- 三块统计 ----
  const tc = {};
  NOTICES.forEach(r => (r["通知类型"]||[]).forEach(t => { tc[t] = (tc[t]||0) + 1; }));
  const items = Object.keys(tc)
    .sort((a,b) => (TYPE_ORDER.indexOf(a) === -1 ? 99 : TYPE_ORDER.indexOf(a)) - (TYPE_ORDER.indexOf(b) === -1 ? 99 : TYPE_ORDER.indexOf(b)))
    .map(t => [t, tc[t], TYPE_COLOR[t] || "var(--faint)"]);
  bars(document.getElementById("ntByType"), items.length ? items : [["暂无数据", 0, "var(--faint)"]]);

  // v0.22.0：原来这儿还有两块面板 ——「截止紧迫度」（四档里常常三个是 0、第四个就是
  // 「已处理」，跟上面那张卡重复）和「处理进度」（那个 97% 跟「已处理 3x」是同一个数）。
  // 她嫌顶部乱，两块都砍了，只留唯一有信息量的「通知类型分布」。
  // 面板里那几个数（涉及几家公司 / 配对上了多少 / 最近一个待办）并进表格下面那句 note。
  const comps = new Set(NOTICES.map(r => r["公司名称"]).filter(Boolean));
  const matched = NOTICES.filter(r => appOf(r)).length;
  const soonest = open.filter(r => daysLeft(r["通知截止时间"]) !== null)
    .sort((a,b) => daysLeft(a["通知截止时间"]) - daysLeft(b["通知截止时间"]))[0];

  const any = ntQuick !== "all" || ntType || ntQ || ntHideDone;
  const nConds = ntConds();
  const ntResetEl = document.getElementById("ntReset");
  ntResetEl.hidden = !any;
  // 按钮上带出「几项」—— 她点过的东西自己会忘，数字会提醒她表正被筛着
  ntResetEl.textContent = nConds.length > 1 ? "清除筛选（" + nConds.length + " 项）" : "清除筛选";
  // 归纳形态下两个口径都报（家数 + 条数），跟机会库 total/total_rows 一个道理：
  // 只报一个，她对着表数出来对不上就要来问
  document.getElementById("ntCount").textContent = any
    ? (ntGrouped ? "筛出 " + groups.length + " 家 · " + list.length + " / 共 " + n + " 条"
                 : "筛出 " + list.length + " / 共 " + n + " 条") +
      (nConds.length ? "（" + nConds.join(" · ") + "）" : "")
    : (ntGrouped ? "共 " + groups.length + " 家 · " + n + " 条" : "共 " + n + " 条");

  const noteEl = document.getElementById("ntNote");
  // 「涉及 N 家公司 / M 条通知对上了投递 / 最近一个待办」原来在顶部那块「处理进度」面板里，
  // 面板砍掉之后并到这儿 —— 信息不丢，也不占最上面那屏。
  const stats = n
    ? '<br>涉及 ' + comps.size + ' 家公司 · ' + matched + ' / ' + n + ' 条通知对上了投递记录' +
      (soonest ? " · 最近一个待办是 " + esc(soonest["公司名称"]) + "（" + esc(soonest["通知截止时间"] || "—") + "）" : "")
    : "";
  if(META.sync_bad){
    noteEl.innerHTML = "邮件同步失败（" + esc(META.sync_msg || "") + "）——下面显示的是上次成功同步的数据。" + stats;
  }else if(META.notices_mode === "feishu"){
    noteEl.innerHTML = "这张表直接从飞书多维表格读，每 10 秒重读本地缓存。" + esc(META.sync_msg || "") +
      "。<br>「处理」那一列是下拉框，改完立刻生效，存在本机 <code>data/notice_states.json</code> 里——" +
      "飞书那边同步新数据时不会把你的改动冲掉。选「（清除我的标记）」就回到飞书原来的值。" +
      "<br>标成「已完成 / 已过期 / 已忽略」的都不再计入逾期，只会沉到表格最后，" +
      "「距截止」那一列也不再报还剩几天。" +
      (META.notice_overrides ? "现在有 " + META.notice_overrides + " 条是你手动标的。" : "") + stats;
  }else if(META.notices_mode === "imap"){
    noteEl.innerHTML = "这张表是本机直连邮箱（" + esc(META.notices_source) + "）收来的，每 10 秒重读一次。" +
      "只认五类通知：资料补充 / AI面 / 笔试 / 测评 / 面试；拿不准的进待确认区、认不出公司的进上面的提示条，都不会乱标。" +
      "<br>「处理」那一列是你自己标的，存在本机 <code>data/notice_states.json</code> 里，下次收邮件不会被冲掉。" +
      (META.notice_overrides ? "现在有 " + META.notice_overrides + " 条是你手动标的。" : "") + stats;
  }else if(META.notices_ok){
    noteEl.innerHTML = "这张表的数据来自本机的通知 CSV（" + esc(META.notices_source) + "），每 10 秒重读一次。" + stats;
  }else{
    noteEl.innerHTML = "还没接上邮箱数据：点上面的「连接我的邮箱」按钮（不用开终端），" +
      "或在终端跑 <code>python3 applytracker.py --mail-config</code>。";
  }
  // 两个视图都铺完（进度那个也在下面这一句里铺）
  renderIvView();
}

// ---------------- 面试进度（v0.21.0：表格里的一列，不再是页面上单独一块） ----------------
// 数据还是「一家公司一条」：环节顺序 + 当前环节存在本机 data/interview_progress.json，
// 没手改过的按通知自动推断；这里只是把它画到**这家公司的每一条 AI面 / 面试 通知**行上。
// 交互：点已达成的环节 = 把当前改到那；点灰块 = 往前走一环；「改」= 这家流程不一样时自己排。
let ivEditKey = "";   // 哪一家的编辑器正展开着（按公司的 key 记）

// —— 这一页的进度筛选（v0.24.0）——
// 她 2026-10-05 提的「这一页也要可筛选进度」：一排环节按钮，点一下只看停在这一环的公司。
// 选中值就是**环节名本身**，另加一个「已止步」伪值（挂了的那几家单独看）。
// 两个伪值拿双下划线打头 —— 环节名不可能长这样，不会撞。
const IV_FKEY = "at.ivf.v1";
const IV_ALL = "__all__", IV_STOP = "__stop__";
// 环节先后由后端给（测评 → 笔试 → AI面试 → 群面 → 一面…），前端别自己排 ——
// 中文按字典序排出来是错的（「一面」会跑到「测评」前面）。
// 认不出的环节兜到最后：排序值给大数，别让「indexOf == -1」把它们顶到最前面去。
const IV_ORDER = META.iv_order || ["测评", "笔试", "AI面试", "群面", "一面", "二面", "三面", "终面", "HR面", "offer"];
const ivOrderOf = n => { const i = IV_ORDER.indexOf(n); return i < 0 ? 999 : i; };
let ivNode = IV_ALL, ivQ = "", ivFLoaded = false;
function ivSave(){
  if(!ivFLoaded) return;
  lsSave(IV_FKEY, {node: ivNode, q: ivQ});
}
function ivLoad(){
  const d = lsLoad(IV_FKEY);
  ivFLoaded = true;
  if(!d) return;
  if(typeof d.node === "string") ivNode = d.node;
  if(typeof d.q === "string") ivQ = d.q;
  const el = document.getElementById("ivQ");
  if(el) el.value = ivQ;
}

function ivBar(it){
  const nodes = it["节点"] || [], cur = it["当前"] || "";
  const ci = cur ? nodes.indexOf(cur) : -1;
  const stop = !!it["止步"];
  let h = "";
  nodes.forEach((n, i) => {
    // 没到的整段折成一个灰块，只画第一个；已经止步的连灰块都不画 ——
    // 那几轮不会再来了，留着一条灰色路径只会让人误以为还有戏
    if(i > ci + 1 || (stop && i > ci)) return;
    if(i > 0) h += '<i class="ivl' + (i <= ci ? " ok" : "") + '"></i>';
    if(i < ci){
      // 已经过的环节也能点 —— 点一下把「现在走到哪」**退回来**（推错了 / 面完了要回退一把）
      // 2026-10-08 无障碍 A1：这些环节本来就是按钮（能点推进 / 退回），原来却是 <span> 挂 onclick，
      // 键盘够不着。改原生 <button>，焦点与 Enter/Space 白拿，不用自己监听键盘。
      h += '<button type="button" class="ivn ok" data-node="' + esc(n) +
        '" title="已经过了 —— 点一下把当前退回「' + esc(n) + '」">' + esc(n) + '</button>';
    }else if(i === ci){
      const cls = "ivn cur" + (n === "offer" ? " offer" : "") + (it["待确认"] ? " ask" : "");
      h += '<button type="button" class="' + cls + '" data-node="' + esc(n) + '" title="' +
        (it["待确认"] ? "这封邮件没写第几轮，点一下改成实际的轮次"
                      : "当前走到「" + esc(n) + "」，点别的环节可以改") + '">' +
        esc(n) + (it["待确认"] ? '<em>?</em>' : '') + '</button>';
    }else{
      const rest = nodes.slice(ci + 1);
      const label = rest.length > 1 ? (n + "—" + rest[rest.length - 1]) : n;
      h += '<button type="button" class="ivn next" data-node="' + esc(n) + '" title="还没到：' + esc(rest.join(" / ")) +
        "（点一下把当前推进到「" + esc(n) + '」）">' + esc(label) + '</button>';
    }
  });
  // 挂了的，在进度条**末尾**挂一个红标（v0.24.1 她 2026-10-05：「已结束的在进度条后面加一个已挂，红色底色」）。
  // 原来只用「整行灰化 + 下面一行小字」表达止步 —— 她的口径是「更美观 = 要看得见」，
  // 淡下去恰恰是最看不见的一种做法。红底是整块列表里唯一的红，扫一眼就能挑出来。
  if(stop){
    h += '<span class="ivdead" title="' + (it["止步来源"] === "已结束"
      ? "投递表里这家标了「已结束」"
      : (it["止步来源"] === "邮件" ? "邮件里的拒信自动标的止步"
        : "你手动标的止步")) + '">已挂</span>';
  }
  return h;
}

function ivEditor(it){
  const stop = it["止步"];
  return '<div class="ived" data-key="' + esc(it["key"]) + '">' +
    '<span class="lbl">环节（按顺序、用「、」隔开）</span>' +
    '<input type="text" class="ivnin" value="' + esc((it["节点"] || []).join("、")) + '">' +
    '<button class="primary ivsave">保存</button>' +
    '<button class="ivcancel">取消</button>' +
    '<button class="ivstop">' + (stop ? "继续推进（取消止步）" : "止步于此") + '</button>' +
    (it["来源"] === "手动" ? '<button class="ivreset">恢复自动</button>' : "") +
    '</div>';
}

// ---- 第二个视图的渲染（v0.22.0；v0.24.0 加进度筛选）----
// 一家一行：公司名 + 投递状态 + 进度条 + 「止步」+「改」。进度条是**这家公司**的，
// 所以同一家不会再像上一版那样在好几条通知上重复画好几遍。
function renderIvView(){
  const box = document.getElementById("ivList");
  if(!box) return;
  const base = ivShowList();
  const live = base.filter(x => !x["止步"]).length;
  const stopN = base.length - live;
  const offers = base.filter(x => (x["当前"] || "") === "offer").length;
  const kpiEl = document.getElementById("ivKpi");
  if(kpiEl){
    kpiEl.innerHTML =
      kpi(live, "在推进", "t-active", "", false, "还没止步的") +
      kpi(offers, "拿到 offer", offers ? "t-done" : "t-total", "", false, offers ? "走到最后一环" : "") +
      kpi(stopN, "已止步", stopN ? "t-over" : "t-total", "", false, stopN ? "挂了 / 不再往下推的" : "");
    kpiEl.querySelectorAll(".kpi-card").forEach(el => { el.onclick = null; });
  }

  // —— 环节筛选按钮：按当前数据现取，没走到那一环的公司不存在就不摆那个按钮 ——
  const nodes = [];
  base.forEach(it => { const c = it["当前"] || ""; if(c && nodes.indexOf(c) < 0) nodes.push(c); });
  nodes.sort((a, b) => ivOrderOf(a) - ivOrderOf(b));
  // 存档里选的环节要是已经没了（那家公司往后走了），回落成「全部」——
  // 跟通知表那边对「已不存在的类型」的处理一个道理，别让她打开看见一张空列表
  if(ivNode !== IV_ALL && ivNode !== IV_STOP && nodes.indexOf(ivNode) < 0) ivNode = IV_ALL;
  if(!stopN && ivNode === IV_STOP) ivNode = IV_ALL;
  const chipsEl = document.getElementById("ivNodeChips");
  if(chipsEl){
    chipsEl.innerHTML = "";
    const mk = (val, label, tip) => {
      const b = document.createElement("button");
      b.textContent = label;
      if(tip) b.title = tip;
      b.classList.toggle("on", ivNode === val);
      b.onclick = () => { ivNode = (ivNode === val) ? IV_ALL : val; renderIvView(); };
      chipsEl.appendChild(b);
    };
    mk(IV_ALL, "全部", "不看环节，全都列出来");
    nodes.forEach(n => mk(n, n, "只看现在停在这一环的公司"));
    if(stopN) mk(IV_STOP, "已止步", "只看挂了 / 不再往下推的（" + stopN + " 家）");
  }
  const list = base.filter(it => {
    if(ivNode === IV_STOP){ if(!it["止步"]) return false; }
    else if(ivNode !== IV_ALL && (it["当前"] || "") !== ivNode) return false;
    if(ivQ){
      const hay = ((it["公司"] || "") + (it["岗位"] || []).join("")).toLowerCase();
      if(!hay.includes(ivQ)) return false;
    }
    return true;
  });
  const cntEl = document.getElementById("ivCount");
  if(cntEl) cntEl.textContent = (list.length === base.length)
    ? "共 " + base.length + " 家"
    : "筛出 " + list.length + " / " + base.length + " 家";
  const rstEl = document.getElementById("ivReset");
  if(rstEl) rstEl.hidden = (ivNode === IV_ALL && !ivQ);
  ivSave();

  box.innerHTML = "";
  // 进度条怎么用，写在这儿（这块以前是空的）。她 2026-10-04 问过「能不能回退」——
  // 说明光靠 hover 提示不够，得有一句白纸黑字。
  const ivNote = document.getElementById("ivNote");
  if(ivNote) ivNote.innerHTML = '进度条上每个环节都能点：<b>点还没到的环节 = 往前推进</b>；' +
    '<b>点已经过的环节 = 退回来</b>（点错了能改）；点当前那个不动。<br>' +
    '这家不推了 / 已经挂了，点右边的<b>「止步」</b>——止步的会沉到最后、进度条末尾挂一个<b>红「已挂」</b>，后面那截也不再画。<br>' +
    '这家流程跟别人不一样（没有 AI 面、多了三面…）就点<b>「改」</b>自己排环节。';
  if(!list.length){
    box.innerHTML = base.length
      ? '<div class="ivrow"><div class="ivco"><b>这个筛选下没有公司</b>' +
        '<span>换一个环节，或者点右边「清除筛选」回到全部。</span></div></div>'
      : '<div class="ivrow"><div class="ivco"><b>还没有进到面试的公司</b>' +
        '<span>收到 AI面 / 面试 类通知、或者投递状态走到面试之后，这里就会出现这家公司的进度条。' +
        '只停在「测评」阶段的不显示 —— 这个视图是拿来看面试进展的（不过挂了的会列出来）。</span></div></div>';
  }else{
    list.forEach(it => {
      const a = APPS.filter(x => sameCompanyTight(x["公司名称"], it["公司"]))[0];
      const row = document.createElement("div");
      row.className = "ivrow" + (it["止步"] ? " stopped" : "");
      row.dataset.ikey = it["key"];
      const sub = it["止步"]
        ? (it["止步来源"] === "已结束" ? "已止步 · 投递表标的已结束" : "已止步")
        : "在推进";
      // 拒信 / Offer 邮件的凭据：主题或链接至少留一样，行上看得见「凭什么」（她 2026-10-08 要的）
      const ev = it["邮件依据"] || {};
      const evh = (ev["主题"] || ev["链接"])
        ? '<span class="ivev"> · 邮件' + esc(ev["类型"] || "通知") + '：' +
          (ev["链接"]
            ? '<a href="' + esc(ev["链接"]) + '" target="_blank" rel="noopener" title="打开这封邮件留的链接">'
              + esc(ev["主题"] || ev["链接"]) + '</a>'
            : esc(ev["主题"] || "")) + '</span>'
        : "";
      // 「止步」按钮：投递表里标了已结束的就不给按钮了 —— 那个事实的源头在投递表，
      // 这儿再给一个「继续」让它跟投递表对不上，只会越看越糊涂（想恢复就去那张表改状态）。
      const halt = (it["止步来源"] === "已结束")
        ? '<span class="ivhaltnote" title="投递表里这家标了「已结束」。想让它重新算在推进，去投递表把状态改回来">投递表已结束</span>'
        : '<button class="ivbtn ivhalt" data-key="' + esc(it["key"]) + '" title="' +
          (it["止步"] ? "点一下重新算「在推进」（后面那几环又会画出来）" : "这家挂了 / 不打算再推了 —— 点一下标止步") +
          '">' + (it["止步"] ? "继续" : "止步") + '</button>';
      row.innerHTML =
        '<div class="ivco"><b title="点公司名 → 切回「面试测评截止」并只看这家">' + esc(it["公司"]) + '</b>' +
          '<span>' + (a ? esc(a["状态"]) + " · " : "") + sub + evh +
          (it["来源"] === "手动" && !it["止步"] ? " · 手动改过" : "") + '</span></div>' +
        '<div class="ivbar">' + ivBar(it) + '</div>' +
        '<div class="ivacts">' + halt +
          '<button class="ivbtn ivedit" data-key="' + esc(it["key"]) +
            '" aria-expanded="' + (ivEditKey === it["key"] ? "true" : "false") +
            '" title="这家流程不一样？自己排环节">' +
            (ivEditKey === it["key"] ? "收起" : "改") + '</button></div>';
      box.appendChild(row);
      // 编辑器整条铺在公司那行下面（输入框要占满一行，挤在行里展不开）
      if(ivEditKey === it["key"]) box.insertAdjacentHTML("beforeend", ivEditor(it));
    });
  }
  bindIvView(box);
}

async function ivPost(payload, msg){
  if(OFFLINE){ say("这是离线样例，改不了 —— 打开本地服务再改"); return false; }
  try{
    const r = await fetch("/api/interview", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)});
    const d = await r.json().catch(() => ({}));
    if(!d.ok){ say("没存上：" + (d.error || r.status)); return false; }
    await pull(false);   // 进度条就在第二个视图里，pull 会把两个视图一起重铺
    if(msg) say(msg);
    return true;
  }catch(e){ say("连不上本地服务：" + e.message); return false; }
}

// 进度视图里的交互（这块每次重铺，所以绑事件得在 renderIvView 铺完之后）
function bindIvView(box){
  const itemOfRow = el => {
    const row = el.closest(".ivrow");
    return row ? (IVS.filter(x => x["key"] === row.dataset.ikey)[0] || null) : null;
  };
  // 点环节改「现在走到哪」：往前点推进、点**已经过**的环节退回来、点当前那个不重复发请求
  box.querySelectorAll(".ivn.cur,.ivn.next,.ivn.ok").forEach(el => {
    el.onclick = async () => {
      const it = itemOfRow(el);
      if(!it) return;
      const node = el.dataset.node;
      if(!node) return;
      if(node === (it["当前"] || "")){ say("当前就停在「" + node + "」"); return; }
      const back = el.classList.contains("ok");
      const ahead = el.classList.contains("next");
      await ivPost({key: it["key"], company: it["公司"], nodes: it["节点"], cur: node},
        (back ? "退回「" : ahead ? "推进到「" : "当前改成「") + node + "」，记下了");
    };
  });
  box.querySelectorAll(".ivedit").forEach(b => {
    b.onclick = () => {
      ivEditKey = (ivEditKey === b.dataset.key) ? "" : b.dataset.key;
      renderIvView();
    };
  });
  // 一键止步 / 继续（她 2026-10-05：「示例涂料公司已经给我挂了」——
  // 原来得点「改」再点「止步于此」，两跳；挂了这件事一句话就该办完）。
  // 只发 stop，**不带 nodes / cur** —— 这样不会把这家变成「手动改过」，
  // 已经识别出来的当前环节也照旧跟着通知自动走。点错了再点一下「继续」就回来。
  box.querySelectorAll(".ivhalt").forEach(b => {
    b.onclick = async () => {
      const it = IVS.filter(x => x["key"] === b.dataset.key)[0];
      if(!it) return;
      const stop = !it["止步"];
      await ivPost({key: it["key"], company: it["公司"], stop: stop},
        stop ? "「" + it["公司"] + "」标成止步了（再点一下「继续」能回来）"
             : "「" + it["公司"] + "」又能继续推进了");
    };
  });
  // 点公司名 → 切回「面试测评截止」并只看这一家（原来那个视图里也是这么跳的）
  box.querySelectorAll(".ivco b").forEach(b => {
    b.onclick = () => {
      const it = itemOfRow(b);
      if(!it) return;
      const name = String(it["公司"] || "");
      ntQ = name.toLowerCase();
      const q = document.getElementById("ntQ");
      if(q) q.value = name;
      ntQuick = "all"; ntHideDone = false;
      setNtView("dl");
      renderNotices();
    };
  });

  const ed = box.querySelector(".ived");
  if(!ed) return;
  const it = IVS.filter(x => x["key"] === ed.dataset.key)[0];
  if(!it) return;
  const inp = ed.querySelector(".ivnin");
  const save = async () => {
    const nodes = inp.value.split(/[、,，\/\s]+/).map(s => s.trim()).filter(Boolean);
    if(!nodes.length){ say("环节不能是空的"); return; }
    if(await ivPost({key: it["key"], company: it["公司"], nodes: nodes}, "环节改好了")){
      ivEditKey = "";
    }
  };
  ed.querySelector(".ivsave").onclick = save;
  inp.onkeydown = e => { if(e.key === "Enter") save(); };
  ed.querySelector(".ivcancel").onclick = () => { ivEditKey = ""; renderIvView(); };
  ed.querySelector(".ivstop").onclick = async () => {
    const stop = !it["止步"];
    await ivPost({key: it["key"], company: it["公司"], nodes: it["节点"], cur: it["当前"], stop: stop},
      stop ? "标记成止步了（后面那一截不再显示）" : "又能继续推进了");
  };
  const rst = ed.querySelector(".ivreset");
  if(rst) rst.onclick = async () => {
    if(!confirm("把「" + it["公司"] + "」的进度改回按通知自动推断？你手动改的环节和当前环节都会清掉。")) return;
    if(await ivPost({key: it["key"], reset: true}, "已恢复自动推断")){
      ivEditKey = "";
    }
  };
}

// ---- 这一页两个视图的切换（v0.22.0）----
// 她 2026-10-03 看完「进度条并进表里」那版之后自己定的：还是分成两个视图看更清爽。
// ⚠️ 跟 v0.20.1 那次「互斥切换」**不是一回事**：那次是我把她已经有的一块**藏起来**
// （她以为功能没了，第二天就否了）；这次两个视图各自完整、各自有入口，
// 切过去只是为了换个角度看同一批数据。改这块之前先把这条区别想清楚。
const NT_VKEY = "at.ntv.v1";
let ntTab = document.documentElement.getAttribute("data-ntv") === "iv" ? "iv" : "dl";
document.documentElement.setAttribute("data-ntv", ntTab);
function setNtView(v){
  ntTab = (v === "iv") ? "iv" : "dl";
  document.documentElement.setAttribute("data-ntv", ntTab);
  const a = document.getElementById("ntvDl"), b = document.getElementById("ntvIv");
  if(a) a.classList.toggle("on", ntTab === "dl");
  if(b) b.classList.toggle("on", ntTab === "iv");
  lsSave(NT_VKEY, ntTab);
}
document.getElementById("ntvDl").onclick = () => setNtView("dl");
document.getElementById("ntvIv").onclick = () => { setNtView("iv"); renderIvView(); };
setNtView(ntTab);   // 按钮点亮态跟上（属性已经在 head 里定过了）

document.getElementById("ntQ").oninput = e => { ntQ = e.target.value.trim().toLowerCase(); renderNotices(); };
document.getElementById("ntReset").onclick = () => { ntClearFilters(); };
// 进度视图自己的筛选（v0.24.0）：搜公司 + 清除。跟通知表那边互不干扰 ——
// 两个视图看的是同一批数据，但筛的东西不一样，共用一个搜索词只会互相搅。
ivLoad();   // 必须排在第一次 renderIvView() 之前
const ivQEl = document.getElementById("ivQ");
if(ivQEl) ivQEl.oninput = e => { ivQ = e.target.value.trim().toLowerCase(); renderIvView(); };
document.getElementById("ivReset").onclick = () => {
  ivNode = IV_ALL; ivQ = "";
  const q = document.getElementById("ivQ");
  if(q) q.value = "";
  renderIvView();
};
[["ntSortUrg","urg"],["ntSortDl","dl"],["ntSortCo","co"]].forEach(pair => {
  document.getElementById(pair[0]).onclick = () => { ntSort = pair[1]; renderNotices(); };
});
document.getElementById("ntHideDone").onclick = () => { ntHideDone = !ntHideDone; renderNotices(); };
// 「按公司归纳」开关：默认开（她拍板的形态）；关掉回到一行一封的旧样子，两种形态都在
document.getElementById("ntGrouped").onclick = () => { ntGrouped = !ntGrouped; renderNotices(); };

// ---------------- 页面直连邮箱（她 2026-10-08：「小白也能连」，不开终端） ----------------
// 前端这份域名表只管**自动填格子**（体验）；服务端 /api/mail-setup 里还有同一份，
// 以服务端那份为准 —— 两边都认不出的，让用户选「其他邮箱」手填，绝不瞎猜。
const MAIL_HOST_MAP_JS = {
  "qq.com": ["imap.qq.com", 993], "foxmail.com": ["imap.qq.com", 993],
  "163.com": ["imap.163.com", 993], "126.com": ["imap.126.com", 993],
  "gmail.com": ["imap.gmail.com", 993],
  "outlook.com": ["outlook.office365.com", 993],
  "hotmail.com": ["outlook.office365.com", 993], "live.com": ["outlook.office365.com", 993],
};
function mailSetupInfer(){
  const em = document.getElementById("mstEmail").value.trim().toLowerCase();
  const dom = em.split("@")[1] || "";
  const hit = MAIL_HOST_MAP_JS[dom];
  document.getElementById("mstHost").value = hit ? hit[0] : "";
  document.getElementById("mstPort").value = hit ? String(hit[1]) : "993";
}
function openMailSetup(){
  document.getElementById("mailSetupWrap").hidden = false;
  document.getElementById("mstErr").hidden = true;
  document.getElementById("mstEmail").focus();
}
function closeMailSetup(){ document.getElementById("mailSetupWrap").hidden = true; }
document.getElementById("mstEmail").addEventListener("input", mailSetupInfer);
// 「其他邮箱」才放开手填；选回自动推断就清掉手填值重新按邮箱算
document.getElementById("mstProv").onchange = () => {
  const other = document.getElementById("mstProv").value === "other";
  document.getElementById("mstHost").readOnly = !other;
  document.getElementById("mstPort").readOnly = !other;
  if(!other) mailSetupInfer();
  else { document.getElementById("mstHost").value = ""; document.getElementById("mstPort").value = "993"; }
};
// Esc 关卡 —— 只挂在卡内输入框上（无障碍 A1 钉过：不许加全局键盘监听）
["mstEmail", "mstCode", "mstHost", "mstPort"].forEach(id => {
  document.getElementById(id).onkeydown = (e) => { if(e.key === "Escape") closeMailSetup(); };
});
document.getElementById("mstCancel").onclick = closeMailSetup;
document.getElementById("mstGo").onclick = () => {
  const err = document.getElementById("mstErr");
  const go = document.getElementById("mstGo");
  const other = document.getElementById("mstProv").value === "other";
  err.hidden = true;
  go.disabled = true;
  fetch("/api/mail-setup", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      email: document.getElementById("mstEmail").value.trim(),
      authcode: document.getElementById("mstCode").value,
      host: other ? document.getElementById("mstHost").value.trim() : "",
      port: other ? document.getElementById("mstPort").value.trim() : ""
    })})
    .then(r => r.json()).then(d => {
      if(d.ok){
        closeMailSetup();
        document.getElementById("mstCode").value = "";   // 授权码不留痕
        // 说人话：连上 ≠ 已启用。她连完邮箱时面板读的可能还是飞书表 ——
        // 不能一律喊「正在收第一轮邮件」骗她（2026-10-08 复盘：那句话就是撒谎的源头）。
        if(d.switched === true){
          say("邮箱已连接，正在收第一轮邮件…");
        }else{
          say("邮箱已连上（备用）。这张表现在读的是" + noticesSrcName() +
              "，要改成直连就点「改用直连收信」。");
        }
        pull();
      }else{
        err.textContent = d.error || "连接失败";
        err.hidden = false;
      }
    })
    .catch(e => { err.textContent = "连不上本地服务：" + e.message; err.hidden = false; })
    .finally(() => { go.disabled = false; });
};
document.getElementById("ntOnlyTodo").onclick = () => {
  ntQuick = (ntQuick === "todo") ? "all" : "todo";
  renderNotices();
};
// 「只看紧急的」= 原来顶部那条「待办 … 只看这些」按钮。提示条 v0.22.0 砍了，但能力留着 ——
// 它筛的是「已逾期 + 今天截止 + 3 天内」，跟「只看没处理的」不是一回事。
document.getElementById("ntUrgent").onclick = () => {
  ntQuick = (ntQuick === "urgent") ? "all" : "urgent";
  if(ntQuick === "urgent") ntHideDone = false;
  renderNotices();
};

// ---------------- 机会库 ----------------
// 数据有好几千条，不进首屏：切到这个标签页才去 /api/opps 拿，一次 60 条
// 搜索条件：每条是 {f: 字段键, v: 小写词串}。数组里永远至少留一条（空的）。
// 字段列表由后端白名单注入，页面里不再另抄一份。
const OPP_FIELD_OPTS = __FIELDS__;
const OPP_FIELD_NAME = {};
OPP_FIELD_OPTS.forEach(o => { OPP_FIELD_NAME[o[0]] = o[1]; });
let oppInc = [{f:"", v:""}];     // 「包含」条件，可以挂多条
let oppExc = [{f:"", v:""}];     // 「不含」条件，同样可以挂多条
let oppRev = false;
let oppInd = "", oppCity = "", oppNat = "", oppBatch = "", oppTarget = "", oppExam = "", oppCo = "";
let oppSort = "dl";
let oppOpen = true, oppMine = false, oppLimit = 60, oppTimer = null;
// 「最近新开」：0 = 不看；1 / 3 / 7 = 只看最近这几天内更新过的。点一下换档位、转一圈回到关。
const OPP_FRESH_STEPS = [0, 7, 3, 1];
let oppNew = 0;
const OPP_DET = {};    // 展开了详情的行（按记录 id）
const OPP_GRP = {};    // 展开了「这家公司下有几条」的组（按归并键）
const FBI = {};        // 分布面板是否「展开全部」
const OPP = {total:0, rows:[], groups:[], facets:null, stats:null, all:0, all_groups:0,
             total_rows:0, loaded:false, busy:false, bad:false, state:"", synced_at:"", source:""};

// —— 筛选条件记忆（localStorage）——
// 每次加载机会库都把当前筛选写进去；页面刷新 / 重开浏览器后自动恢复上次的条件。
// 存的是变量本来的形状，读回来前逐项验型，坏数据宁可丢掉也别把页面弄挂。
const OPP_QKEY = "at.opps.q.v1";
const OPP_SORTS = ["dl", "up", "co", "ind"];
let oppQLoaded = false;
function oppSave(){
  if(!oppQLoaded) return;
  lsSave(OPP_QKEY, {
    inc: oppInc, exc: oppExc, ind: oppInd, city: oppCity, nat: oppNat,
    batch: oppBatch, target: oppTarget, co: oppCo, exam: oppExam,
    open: oppOpen, mine: oppMine, rev: oppRev, sort: oppSort, limit: oppLimit, fresh: oppNew
  });
}
// —— 筛选条件记忆 ——
// 每次加载机会库都把当前筛选写进去；页面刷新 / 重开浏览器后自动恢复上次的条件。
// 存的是变量本来的形状，读回来前逐项验型，坏数据宁可丢掉也别把页面弄挂。
function oppLoad(){
  const d = lsLoad(OPP_QKEY);
  oppQLoaded = true;    // 没存档也要放行，否则第一屏的筛选永远存不下来
  if(!d) return;
  try{
    const cond = a => Array.isArray(a)
      ? a.filter(c => c && typeof c === "object" && typeof c.v === "string")
         .map(c => ({f: String(c.f || ""), v: String(c.v || "")}))
      : null;
    const inc = cond(d.inc), exc = cond(d.exc);
    if(inc && inc.length) oppInc = inc;
    if(exc && exc.length) oppExc = exc;
    const s = k => (typeof d[k] === "string" ? d[k] : "");
    oppInd = s("ind"); oppCity = s("city"); oppNat = s("nat"); oppBatch = s("batch");
    oppTarget = s("target"); oppCo = s("co");
    oppSort = OPP_SORTS.indexOf(d.sort) >= 0 ? d.sort : "dl";
    if(d.exam === "含免笔试") oppExam = "含免笔试";
    if(typeof d.open === "boolean") oppOpen = d.open;
    if(typeof d.mine === "boolean") oppMine = d.mine;
    if(typeof d.rev === "boolean") oppRev = d.rev;
    // 档位只在白名单里认（存档被改坏 → 退回「不看」，而不是筛出个空表）
    if(OPP_FRESH_STEPS.indexOf(d.fresh) > 0) oppNew = d.fresh;
    // 「再看 60 条」翻到第几屏也记住；夹在 60~600，存档被改坏也不会一次拉爆
    if(typeof d.limit === "number" && isFinite(d.limit) && d.limit >= 60)
      oppLimit = Math.min(600, Math.floor(d.limit / 60) * 60);
    // 字段键不在白名单里的（以后改过字段名）退回「全部字段」，别拿它筛空整个表
    [oppInc, oppExc].forEach(arr => arr.forEach(c => {
      if(c.f && !OPP_FIELD_OPTS.some(o => o[0] === c.f)) c.f = "";
    }));
    ["Ind","City","Nat","Batch"].forEach(k => {
      const el = document.getElementById("opp" + k);
      if(el) el.value = {Ind:oppInd, City:oppCity, Nat:oppNat, Batch:oppBatch}[k];
    });
  }catch(e){ /* 存档结构不对就当没有，别让整页 JS 停摆 */ }
}

// 某个筛选条件当前选中的值（详情行里高亮用）
function oppCur(k){
  if(k === "ind") return oppInd;
  if(k === "city") return oppCity;
  if(k === "nat") return oppNat;
  if(k === "batch") return oppBatch;
  if(k === "target") return oppTarget;
  if(k === "exam") return oppExam;
  if(k === "co") return oppCo;
  return "";
}

// 点一下筛、再点一下取消。表格里的标签、分布面板、详情行都走这里。
function oppSet(k, v){
  const same = oppCur(k) === v;
  const val = same ? "" : v;
  if(k === "ind") oppInd = val;
  else if(k === "city") oppCity = val;
  else if(k === "nat") oppNat = val;
  else if(k === "batch") oppBatch = val;
  else if(k === "target") oppTarget = val;
  else if(k === "exam") oppExam = val;
  else if(k === "co") oppCo = val;
  else return;
  oppLimit = 60;
  loadOpps();
}

function oppQuery(){
  const p = new URLSearchParams();
  // q/qf、qx/qxf 都是可重复的平行数组：第 i 个词串配第 i 个字段。
  // 只发填了词的条件，所以两个数组长度永远相等。
  oppInc.forEach(c => { if(c.v){ p.append("q", c.v); p.append("qf", c.f); } });
  oppExc.forEach(c => { if(c.v){ p.append("qx", c.v); p.append("qxf", c.f); } });
  if(oppInd) p.set("ind", oppInd);
  if(oppCity) p.set("city", oppCity);
  if(oppNat) p.set("nat", oppNat);
  if(oppBatch) p.set("batch", oppBatch);
  if(oppTarget) p.set("target", oppTarget);
  if(oppExam) p.set("exam", oppExam);
  if(oppCo) p.set("co", oppCo);
  if(oppOpen) p.set("open", "1");
  if(oppNew) p.set("newdays", String(oppNew));
  if(oppMine) p.set("mine", "1");
  if(oppRev) p.set("rev", "1");
  p.set("sort", oppSort);
  p.set("limit", String(oppLimit));
  return p.toString();
}

// —— 离线样例（file:// 打开、没有后端）时的本地筛选 ——
// 口径跟服务端 query_opportunities 对齐：包含要全命中、不含命中任一个就排掉，
// 两边各自可以只在自己选的字段里找。注意样例只嵌了一屏快照，只在快照范围内生效。
const OPP_FIELD_MAP = {ind:"industries", city:"cities", nat:"nature", target:"targets",
                       batch:"batch", exam:"exam"};
function oppFieldText(r, f){
  if(f === "co") return String(r.company || "").toLowerCase();
  if(f === "role") return String(r.roles || "").toLowerCase();
  if(f === "note") return String(r.note || "").toLowerCase();
  if(f === "deadline") return String(r.deadline || "").toLowerCase();
  if(f === "updated") return String(r.updated || "").toLowerCase();
  if(OPP_FIELD_MAP[f]){
    let v = r[OPP_FIELD_MAP[f]];
    if((!v || !v.length) && f === "nat") v = r.nat;    // 老快照里可能只有归并后的 nat
    return (v || []).join(" ").toLowerCase();
  }
  return [r.company, r.roles, r.note, r.deadline].concat(
    ["industries", "nat", "nature", "cities", "targets", "batch", "exam"]
      .map(k => (r[k] || []).join(" "))).join(" ").toLowerCase();
}
// —— 「最近新开」用的时间工具 ——
// 后端会补好 r.upd（时间戳）；离线样例里没有，就在这儿现场解析（源表两种写法都认）。
function oppUpdTS(r){
  if(typeof r.upd === "number") return r.upd;
  const m = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/.exec(String(r.updated || "").trim());
  return m ? new Date(+m[1], +m[2] - 1, +m[3]).getTime() / 1000 : null;
}
// 更新日距今几天（0 = 今天）；认不出来返回 null
function oppUpdAge(r){
  const t = oppUpdTS(r);
  if(t === null) return null;
  const n = new Date();
  const d0 = new Date(n.getFullYear(), n.getMonth(), n.getDate()).getTime() / 1000;
  return Math.round((d0 - t) / 86400);
}
// 行内小徽标：只在最近 3 天内更新过的记录上出现
function oppFreshTag(r){
  const a = oppUpdAge(r);
  if(a === null) return "";
  const d = new Date(oppUpdTS(r) * 1000);
  const ymd = d.getFullYear() + "/" + String(d.getMonth() + 1).padStart(2, "0")
            + "/" + String(d.getDate()).padStart(2, "0");
  if(a <= 0) return '<span class="fresh now" title="今天更新（' + ymd + '）">今天</span>';
  if(a <= 3) return '<span class="fresh" title="' + a + ' 天前更新（' + ymd + '）">' + a + '天前</span>';
  return "";
}
// 按钮文字跟着档位走，一眼看出现在筛的是哪一档
function oppNewLabel(){
  if(!oppNew) return "最近新开";
  return oppNew === 1 ? "🆕 今天更新" : "🆕 " + oppNew + " 天内更新";
}

// 搜索条件是不是「设过」（重置按钮要不要露出来）。字段选了但没填词不算 —— 那不影响结果。
function oppQAny(){
  return oppInc.some(c => c.v) || oppExc.some(c => c.v);
}
// 包含：每条条件里的词都要在它自己选的字段里全命中；条件之间是「且」
function oppIncOk(r){
  for(const c of oppInc){
    const ts = c.v.split(/\s+/).filter(Boolean);
    if(!ts.length) continue;
    const t = oppFieldText(r, c.f);
    if(!ts.every(x => t.includes(x))) return false;
  }
  return true;
}
// 不含：任一条条件命中就整条排掉
function oppExcHit(r){
  for(const c of oppExc){
    const ts = c.v.split(/\s+/).filter(Boolean);
    if(!ts.length) continue;
    if(ts.some(x => oppFieldText(r, c.f).includes(x))) return true;
  }
  return false;
}
function oppSeedFilter(all){
  const now = Date.now() / 1000;
  const mine = (APPS || []).map(a => a["公司名称"]).filter(Boolean);
  let out = all.filter(r => {
    if(oppInd && !(r.industries || []).includes(oppInd)) return false;
    if(oppCity && !(r.cities || []).includes(oppCity)) return false;
    if(oppNat && !(r.nat || []).includes(oppNat)) return false;
    if(oppBatch && !(r.batch || []).includes(oppBatch)) return false;
    if(oppTarget && !(r.targets || []).includes(oppTarget)) return false;
    if(oppExam && !(r.exam || []).includes(oppExam)) return false;
    if(oppCo && !sameCompany(r.company || "", oppCo)) return false;
    if(oppNew){
      const age = oppUpdAge(r);
      if(age === null || age > oppNew - 1) return false;
    }
    if(oppOpen && r.dl != null && r.dl < now) return false;
    if(oppMine && !mine.some(n => sameCompany(r.company || "", n))) return false;
    if(!oppIncOk(r)) return false;
    if(oppExcHit(r)) return false;
    return true;
  });
  if(oppSort === "dl"){
    out.sort((a, b) => ((a.dl == null) - (b.dl == null)) || ((a.dl || 0) - (b.dl || 0)));
    if(oppRev) out.reverse();
  }else{
    const s = r => String(oppSort === "co" ? (r.company || "")
                       : oppSort === "up" ? (r.updated || "")
                       : (r.industries || [])[0] || "");
    out.sort((a, b) => s(a).localeCompare(s(b), "zh"));
    if(oppRev) out.reverse();
  }
  return out;
}

// 把记录按公司归成一堆（离线样例用）。
// 归并规则不在这儿：键 `gk` 是后端算好、跟着每条记录一起发下来的，所以两边口径天然一致，
// 这里只负责按 gk 归堆 + 算出组头那几个数（跟后端 query_opportunities 的组头对齐）。
function oppGroupRows(rows){
  const map = {}, order = [], FIELDS = ["industries","cities","targets","nat","batch","exam"];
  const now = Date.now() / 1000;
  rows.forEach(r => {
    const k = r.gk || r.company || "";
    let g = map[k];
    if(!g){
      g = map[k] = {key:k, company:k, n:0, bn:0, industries:[], cities:[], targets:[],
                    nat:[], batch:[], exam:[], dl:null, upd:null, applies:0, notices:0,
                    rows:[], _all:[], _fut:[]};
      order.push(k);
    }
    g.n++;
    if(/(分行|分公司|支行|营业部|办事处|分厂|分店)$/.test(String(r.company || ""))) g.bn++;
    FIELDS.forEach(f => (r[f] || []).forEach(v => { if(g[f].indexOf(v) < 0) g[f].push(v); }));
    if(typeof r.dl === "number"){ g._all.push(r.dl); if(r.dl >= now) g._fut.push(r.dl); }
    const u = oppUpdTS(r);
    if(u !== null) g.upd = (g.upd === null) ? u : Math.max(g.upd, u);
    if(r.apply) g.applies++;
    if(r.notice) g.notices++;
    g.rows.push(r);
  });
  const out = order.map(k => map[k]);
  // 截止时间取「组里最近的一个还没过期的」—— 列表上该显示最该去投的那个
  out.forEach(g => {
    const pick = g._fut.length ? g._fut : g._all;
    g.dl = pick.length ? Math.min.apply(null, pick) : null;
    delete g._all; delete g._fut;
  });
  if(oppSort === "up"){        // 按更新时间：新的在前（oppRev 就反过来），认不出的一律沉底
    out.sort((a, b) => ((a.upd || 0) - (b.upd || 0)));
    if(!oppRev) out.reverse();
    out.sort((a, b) => (a.upd === null) - (b.upd === null));
  }else if(oppSort === "co" || oppSort === "ind"){
    const s = g => String(oppSort === "co" ? (g.company || "") : (g.industries || [])[0] || "");
    out.sort((a, b) => s(a).localeCompare(s(b), "zh"));
    if(oppRev) out.reverse();
  }else{                       // 按截止时间：认不出的（招满为止）永远沉底，正反序都一样
    out.sort((a, b) => ((a.dl == null) - (b.dl == null)) ||
                       (oppRev ? ((b.dl || 0) - (a.dl || 0)) : ((a.dl || 0) - (b.dl || 0))));
  }
  return out;
}

async function loadOpps(){
  oppSave();   // 能走到这里就是筛选刚变过（或首次载入），把条件记下来
  // 离线样例里带了快照，就用它，并且在本机把筛选做掉（没有后端可问）
  if(OPP_SEED && OPP_SEED.rows && OPP_SEED.rows.length){
    if(!OPP.loaded) Object.assign(OPP, OPP_SEED, {loaded:true, busy:false, bad:false, state:"", seed:true});
    const rows = oppSeedFilter(OPP_SEED.rows);
    const groups = oppGroupRows(rows);
    OPP.rows = rows;
    OPP.groups = groups;
    OPP.total = groups.length;
    OPP.total_rows = rows.length;
    renderOpps();
    return;
  }
  if(OPP.seed){ renderOpps(); return; }
  try{
    const r = await fetch("/api/opps?" + oppQuery(), {cache: "no-store"});
    const d = await r.json();
    Object.assign(OPP, {
      total: d.total || 0, rows: d.rows || [], groups: d.groups || [],
      total_rows: d.total_rows || 0, all_groups: d.all_groups || 0,
      facets: d.facets || null, stats: d.stats || null, all: d.all || 0,
      busy: !!d.busy, bad: !!d.bad,
      state: d.state || "", synced_at: d.synced_at || "", source: d.source || "",
      loaded: true,
    });
    renderOpps();
    // 后台还在拉就过几秒再看一眼，进度条会自己往前走
    if(OPP.busy && document.getElementById("pane-opps").classList.contains("on")){
      clearTimeout(OPP._t);
      OPP._t = setTimeout(loadOpps, 3000);
    }
  }catch(e){ say("机会库加载失败：" + e.message); }
}
function ensureOpps(){ if(!OPP.loaded) loadOpps(); }

function oppDl(r){
  if(!r.dl) return ["招满为止", "none"];
  const d = new Date(r.dl * 1000), n = new Date();
  const days = Math.round((Date.UTC(d.getFullYear(), d.getMonth(), d.getDate())
                         - Date.UTC(n.getFullYear(), n.getMonth(), n.getDate())) / 86400000);
  const ymd = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0")
            + "-" + String(d.getDate()).padStart(2, "0");
  if(days < 0) return ["已过 " + (-days) + " 天", "none"];
  if(days === 0) return [ymd + " · 今天", "soon"];
  if(days <= 7) return [ymd + " · " + days + " 天", "soon"];
  return [ymd, ""];
}

function fillSel(sel, items, cur, label){
  const opts = ['<option value="">' + esc(label) + '</option>'];
  (items || []).forEach(it => {
    opts.push('<option value="' + esc(it[0]) + '"' + (it[0] === cur ? " selected" : "") + '>'
              + esc(it[0]) + "（" + it[1] + '）</option>');
  });
  sel.innerHTML = opts.join("");
}

// 分布面板：一条一行，点一下筛、再点一下取消。取全表所有取值，默认只铺前 10 条。
function facetBars(elId, items, cur, max, onPick){
  const el = document.getElementById(elId);
  const all = items || [];
  const st = FBI[elId] || (FBI[elId] = {open:false});
  const cap = max || 10;
  const list = st.open ? all : all.slice(0, cap);
  // 条宽按全量算，展开前后不会跳
  const top = Math.max(1, ...all.map(x => x[1]));
  let html = st.open && all.length > 24
    ? '<input class="fbin" type="search" placeholder="在这 ' + all.length + ' 项里找…">'
    : "";
  html += list.length
    ? '<div class="fbars' + (st.open ? " fbwrap" : "") + '">' + list.map(it => {
        const on = it[0] === cur;
        return '<button class="fbar' + (on ? " on" : "") + '" data-v="' + esc(it[0]) + '">' +
          '<span class="fb-i"><i style="width:' + Math.max(6, Math.round(it[1] / top * 100)) + '%"></i></span>' +
          '<span class="fb-l" title="' + esc(it[0]) + '">' + esc(it[0]) + '</span>' +
          '<span class="fb-n">' + it[1] + '</span></button>';
      }).join("") + "</div>"
    : '<div class="fbmore">没有数据</div>';
  if(all.length > cap){
    html += '<button class="fbext" data-ext="1" aria-expanded="' + (st.open ? "true" : "false") + '">' +
      (st.open ? "收起，只看前 " + cap + " 项" : "展开全部 " + all.length + " 项") + '</button>';
  }
  el.innerHTML = html;
  el.querySelectorAll(".fbar").forEach(b => {
    b.onclick = () => onPick(b.dataset.v === cur ? "" : b.dataset.v);
  });
  const ext = el.querySelector(".fbext");
  if(ext) ext.onclick = () => { st.open = !st.open; renderOpps(); };
  // 面板内搜索只开关行，不重渲染 —— 不然每敲一个字输入框就失焦
  const bin = el.querySelector(".fbin");
  if(bin){
    bin.oninput = () => {
      const t = bin.value.trim().toLowerCase();
      el.querySelectorAll(".fbar").forEach(b => {
        b.style.display = (!t || (b.dataset.v || "").toLowerCase().indexOf(t) >= 0) ? "" : "none";
      });
    };
  }
}

// 表格里「点一下就能筛」的标签单元格
function tagCell(k, arr, cur, max){
  if(!arr || !arr.length) return '<span style="color:var(--faint)">—</span>';
  return arr.slice(0, max).map(v =>
    '<button class="cellf" data-k="' + k + '" data-v="' + esc(v) + '" title="只看「' + esc(v) + '」">' +
    '<span class="opp-tag' + (k === "ind" ? " ind" : "") + '">' + esc(v) + '</span></button>'
  ).join("") + (arr.length > max ? '<span class="opp-more">+' + (arr.length - max) + '</span>' : "");
}

function detItem(label, html){
  return '<div class="ditem"><em>' + label + '</em><span class="dv">' + html + '</span></div>';
}

function detChips(k, arr){
  if(!arr || !arr.length) return '<span style="color:var(--faint)">—</span>';
  const cur = oppCur(k);
  return '<span class="dtags">' + arr.map(v =>
    '<button class="dtag' + (v === cur ? " on" : "") + '" data-k="' + k + '" data-v="' + esc(v) + '">' +
    esc(v) + '</button>').join("") + '</span>';
}

// 展开行：源表 14 个字段一个不落地摊开，能点的都能筛
function oppDetRow(r){
  const t = v => v ? esc(v) : '<span style="color:var(--faint)">—</span>';
  const link = u => u ? '<a href="' + esc(u) + '" target="_blank" rel="noopener">' + esc(u) + '</a>' : t("");
  const tr = document.createElement("tr");
  tr.className = "detrow";
  tr.innerHTML = '<td colspan="9"><div class="dgrid">' +
    detItem("公司名称", '<span class="dtags"><button class="dtag" data-k="co" data-v="' +
            esc(r.company) + '">' + esc(r.company) + '</button></span>') +
    detItem("行业大类", detChips("ind", r.industries)) +
    detItem("企业性质", detChips("nat", r.nat)) +
    detItem("招聘对象", detChips("target", r.targets)) +
    detItem("批次", detChips("batch", r.batch)) +
    detItem("笔试/测评", detChips("exam", r.exam)) +
    detItem("工作地点", detChips("city", r.cities)) +
    detItem("截止时间", t(r.deadline)) +
    detItem("更新时间", t(r.updated)) +
    detItem("投递方式", link(r.apply)) +
    detItem("官方公告", link(r.notice)) +
    detItem("备注/提示", t(r.note)) +
    detItem("招聘岗位", t(r.roles) + (r.roles_more ? '<span style="color:var(--faint)"> …（原表更长）</span>' : "")) +
    detItem("原表性质", t((r.nature || []).join("、"))) +
    detItem("记录 ID", '<span style="color:var(--faint)">' + esc(r.id) + '</span>') +
  '</div></td>';
  return tr;
}

// 一条记录的稳定 id（详情展开状态靠它记）
function oppRid(r){ return r.id || (String(r.company || "") + "|" + String(r.deadline || "")); }

// 公司主行里「点一下不算展开」的东西：链接、输入框、以及那些点一下要筛的标签按钮。
// 主行整行可点（她的原话：「要去点这个小三角才能展开，需要点这一行就能展开」），
// 所以交互元素必须排除掉，否则点公司名会既筛又展开。
const OPP_INTER = "a,select,input,textarea,.cellf";

// 一行岗位记录。sub=true 是公司展开出来的子行（左边一道竖线 + 圆点，缩进一格）
function oppRow(r, sub){
  const dl = oppDl(r);
  const rid = oppRid(r);
  const tr = document.createElement("tr");
  tr.className = sub ? "sub" : "";
  tr.innerHTML =
    '<td class="c-name">' + (sub ? '<span class="subdot"></span>' : '') +
      '<button class="cellf" data-k="co" data-v="' + esc(r.company) + '" title="只看「' +
        esc(r.company) + '」">' + esc(r.company) + '</button>' + oppFreshTag(r) +
      '<div class="cnats">' + (r.targets || []).map(v =>
        '<button class="cellf" data-k="target" data-v="' + esc(v) + '" title="招聘对象，点一下只看这一届">' +
        '<span class="opp-tag tgt">' + esc(v) + '</span></button>').join("") +
        (r.nat || []).map(v =>
        '<button class="cellf" data-k="nat" data-v="' + esc(v) + '" title="企业性质，点一下只看这一类">' +
        '<span class="opp-tag">' + esc(v) + '</span></button>').join("") +
      '</div></td>' +
    '<td class="c-ind">' + tagCell("ind", r.industries, oppInd, 2) + '</td>' +
    '<td class="c-roles" title="' + esc(r.roles) + '">' + esc(r.roles) +
      (r.roles_more ? '<span style="color:var(--faint)"> …等</span>' : '') + '</td>' +
    '<td class="c-cities">' + tagCell("city", r.cities, oppCity, 2) + '</td>' +
    '<td class="dl ' + dl[1] + '">' + esc(dl[0]) + '</td>' +
    '<td>' + tagCell("batch", r.batch, oppBatch, 2) +
      tagCell("exam", r.exam, oppExam, 1) + '</td>' +
    '<td>' + (r.apply ? '<a href="' + esc(r.apply) + '" target="_blank" rel="noopener">投递</a>' : "—") + '</td>' +
    '<td>' + (r.notice ? '<a href="' + esc(r.notice) + '" target="_blank" rel="noopener">公告</a>' : "—") + '</td>' +
    '<td><button class="detbtn' + (OPP_DET[rid] ? " on" : "") + '" data-det="' + esc(rid) +
      '" aria-expanded="' + (OPP_DET[rid] ? "true" : "false") +
      '" title="展开这条记录的全部字段">' + (OPP_DET[rid] ? "⌃" : "⌄") + '</button></td>';
  return tr;
}

// 一家公司占一行 —— 「同一家公司不同分行放在同一个公司下面」。
// 观感跟投递进度表那套一模一样（公司主行 ▶ + 子行），她不用重新学一套。
function oppGroupRow(g){
  const open = !!OPP_GRP[g.key];
  const branchy = g.bn * 2 >= g.n;     // 组里过半的名字带「分行 / 分公司 / 营业部」→ 说「个分行」
  const dl = oppDl({dl: g.dl});
  // 主行第二行的短名：分支机构取「去掉公司名之后剩下的那截」（上海分行 / 北京分行），
  // 同一家公司投了好几个岗位的（比如 ×27）就改成显示岗位名。
  const shorts = g.rows.map(r => branchy
    ? (String(r.company || "").slice(g.key.length) || "本部")
    : (String(r.roles || "").split(/[、,，;；\n]/)[0] || "（没写岗位）"));
  const roles = [];
  g.rows.forEach(r => { const s = String(r.roles || "").trim(); if(s && roles.indexOf(s) < 0) roles.push(s); });
  const withApply = g.rows.filter(r => r.apply)[0];
  const withNotice = g.rows.filter(r => r.notice)[0];
  const tagBtn = (k, v, cls) =>
    '<button class="cellf" data-k="' + k + '" data-v="' + esc(v) + '" title="点一下只看「' + esc(v) + '」">' +
    '<span class="opp-tag' + (cls || "") + '">' + esc(v) + '</span></button>';
  const tr = document.createElement("tr");
  tr.className = "grp" + (open ? " open" : "");
  tr.innerHTML =
    '<td class="c-name">' +
      '<button class="gtoggle" aria-expanded="' + (open ? "true" : "false") +
        '" title="点这一行' + (open ? "收起" : "展开") + '这家的 ' + g.n + ' 条">' +
        '<span class="arw">▶</span><span>' + esc(g.company) + '</span></button>' + oppFreshTag({upd: g.upd}) +
      '<span class="gmeta"><span class="gn">' + g.n + (branchy ? " 个分行" : " 个岗位") + '</span>' +
        '<span class="gpos" title="' + esc(shorts.join(" / ")) + '">' + esc(shorts.join(" · ")) + '</span></span>' +
      '<div class="cnats">' + (g.targets || []).map(v => tagBtn("target", v, " tgt")).join("") +
        (g.nat || []).map(v => tagBtn("nat", v)).join("") + '</div></td>' +
    '<td class="c-ind">' + tagCell("ind", g.industries, oppInd, 2) + '</td>' +
    '<td class="c-roles" title="' + esc(roles.join(" / ")) + '">' + esc(roles[0] || "—") +
      (roles.length > 1 ? '<span class="opp-more">＋' + (roles.length - 1) + ' 类</span>' : '') + '</td>' +
    '<td class="c-cities">' + tagCell("city", g.cities, oppCity, 2) + '</td>' +
    '<td class="dl ' + dl[1] + '" title="这家里最该去投的那个截止时间">' + esc(dl[0]) + '</td>' +
    '<td>' + tagCell("batch", g.batch, oppBatch, 2) + tagCell("exam", g.exam, oppExam, 1) + '</td>' +
    '<td>' + (g.applies > 1 ? '<span class="opp-more">' + g.applies + ' 个入口</span>'
             : (withApply ? '<a href="' + esc(withApply.apply) + '" target="_blank" rel="noopener">投递</a>' : "—")) + '</td>' +
    '<td>' + (g.notices > 1 ? '<span class="opp-more">' + g.notices + ' 个</span>'
             : (withNotice ? '<a href="' + esc(withNotice.notice) + '" target="_blank" rel="noopener">公告</a>' : "—")) + '</td>' +
    '<td></td>';
  // **整行都能点**。⚠️ 别再给里面的按钮单独绑一次 —— 点一下会触发两次（等于没反应），
  // 投递进度表那边踩过这个坑（见 renderApps 的注释）。
  tr.onclick = e => {
    if(e.target && e.target.closest && e.target.closest(OPP_INTER)) return;
    OPP_GRP[g.key] = !open;
    renderOpps();
  };
  return tr;
}

function renderOpps(){
  const f = OPP.facets || {};
  fillSel(document.getElementById("oppInd"), f.industries, oppInd, "全部行业");
  fillSel(document.getElementById("oppCity"), f.cities, oppCity, "全部城市");
  fillSel(document.getElementById("oppNat"), f.nature, oppNat, "全部性质");
  fillSel(document.getElementById("oppBatch"), f.batch, oppBatch, "全部批次");
  facetBars("oppByInd", f.industries, oppInd, 10, v => oppSet("ind", v || ""));
  facetBars("oppByBatch", f.batch, oppBatch, 10, v => oppSet("batch", v || ""));
  facetBars("oppByTarget", f.targets, oppTarget, 10, v => oppSet("target", v || ""));
  facetBars("oppByNat", f.nature, oppNat, 10, v => oppSet("nat", v || ""));

  // 每块搜索的条件行：只刷样式和提示语，绝不重建 DOM —— 那会把正在打字的光标顶掉
  qRowsSync();
  // 离线样例（file:// 打开）没有后端，去飞书同步这条路走不通，按钮就别摆着了
  document.getElementById("oppSync").hidden = OFFLINE;
  document.getElementById("oppOpen").classList.toggle("on", oppOpen);
  document.getElementById("oppMine").classList.toggle("on", oppMine);
  document.getElementById("oppExam").classList.toggle("on", oppExam === "含免笔试");
  const nbEl = document.getElementById("oppNew");
  nbEl.classList.toggle("on", !!oppNew);
  nbEl.textContent = oppNewLabel();
  document.getElementById("oppSortDl").classList.toggle("on", oppSort === "dl");
  document.getElementById("oppSortUp").classList.toggle("on", oppSort === "up");
  document.getElementById("oppSortCo").classList.toggle("on", oppSort === "co");
  document.getElementById("oppSortInd").classList.toggle("on", oppSort === "ind");
  const rb = document.getElementById("oppRev");
  rb.classList.toggle("rev", oppRev);
  rb.textContent = oppRev ? "↓ 反序" : "↑ 正序";
  document.getElementById("oppReset").hidden =
    !(oppQAny() || oppInd || oppCity || oppNat || oppBatch || oppTarget || oppCo || oppMine ||
      oppExam || oppNew || !oppOpen || oppSort !== "dl" || oppRev);

  const tb = document.getElementById("oppTb");
  tb.innerHTML = "";
  if(!OPP.groups.length){
    tb.innerHTML = '<tr><td colspan="9" class="empty">' + (
      OPP.all ? "没有符合条件的岗位（全表 " + OPP.all + " 条，试试放宽筛选）"
      : (OPP.busy ? "正在从飞书拉岗位数据…" + esc(OPP.state || "")
         : (OPP.source === "off"
            ? "还没配机会库数据源：在 config.ini 的 [opportunities] 里填飞书表 token 和 table id"
            : "机会库还是空的，终端里跑一次 <code>--opps-sync</code> 就有了"))) + '</td></tr>';
  }
  OPP.groups.forEach(g => {
    // 一家只有一条的：就跟以前一模一样铺一行（绝大多数都是这种，别为了「统一」给它套个折叠壳）
    const list = g.n <= 1 ? g.rows : (OPP_GRP[g.key] ? g.rows : []);
    if(g.n > 1) tb.appendChild(oppGroupRow(g));
    list.forEach(r => {
      tb.appendChild(oppRow(r, g.n > 1));
      if(OPP_DET[oppRid(r)]) tb.appendChild(oppDetRow(r));
    });
  });

  const st = OPP.stats || {all: OPP.all, open: 0, y2027: 0, mine: 0, exam: 0};
  document.getElementById("oppKpi").innerHTML =
    kpi(st.all, "岗位总数", "t-total", "all", false, OPP.synced_at ? OPP.synced_at + " 同步" : "") +
    kpi(st.y2027, "2027 届能投", "t-active", "y27", oppTarget === "2027届",
        st.all ? Math.round(st.y2027 / st.all * 100) + "% 的岗位" : "") +
    kpi(st.exam, "含免笔试", "t-offer", "exam", oppExam === "含免笔试", "点一下只看这些") +
    kpi(st.open, "还没截止", "t-done", "open", oppOpen, "") +
    kpi(st.new7 || 0, "近 7 天新开", "t-active", "fresh", !!oppNew,
        (st.new7 || 0) ? "点一下只看这些" : "最近 7 天没有更新") +
    kpi(st.mine, "我投过的公司", "t-interview", "mine", oppMine, st.mine ? "点一下只看这些" : "投递记录里还没有对得上的公司") +
    kpi(OPP.total, "筛出公司", "t-soon", "cur", false,
        OPP.total_rows && OPP.total_rows !== OPP.total
          ? "共 " + OPP.total_rows + " 条岗位 / 分行"
          : (oppLimit < OPP.total ? "先显示 " + OPP.groups.length : ""));
  document.querySelectorAll("#oppKpi .kpi-card").forEach(el => {
    el.onclick = () => {
      const q = el.dataset.q;
      if(q === "open") oppOpen = !oppOpen;
      else if(q === "fresh") oppNew = oppNew ? 0 : 7;
      else if(q === "mine") oppMine = !oppMine;
      else if(q === "exam") oppExam = (oppExam === "含免笔试") ? "" : "含免笔试";
      else if(q === "y27") oppTarget = (oppTarget === "2027届") ? "" : "2027届";
      else return;
      oppLimit = 60;
      loadOpps();
    };
  });

  const al = document.getElementById("oppAlert");
  if(OPP.busy){
    al.innerHTML = '<div class="alert"><span class="al">同步中</span><span class="ai">' +
      esc(OPP.state || "正在从飞书拉岗位数据…") + "。这一页会自动刷新。</span></div>";
  }else if(OPP.bad){
    al.innerHTML = '<div class="alert urgent"><span class="al">同步失败</span><span class="ai">' +
      esc(OPP.state) + "——下面显示的是上次同步下来的数据。</span></div>";
  }else{
    al.innerHTML = "";
  }

  document.getElementById("oppMoreBar").hidden = !(OPP.total > OPP.groups.length);
  // 计数看的是「表里显示的到底是不是全表」—— 直接拿真实条数比，不再靠列举筛选条件。
  // 以前这里漏算了默认就开着的「还没截止」：标签写「共 9167 条」，同一屏的
  // 「筛出岗位」卡却写 8104，两个数当着面打架。按数据比就不会再漏。
  // 归并之后要同时说清两个口径：**家数**（表里一行一行数得出来的）和**条数**
  //（源表的记录数，也就是上面 KPI 那个 9381）—— 只报一个必然有人觉得对不上。
  const scopeN = OPP.seed ? ((OPP_SEED.rows || []).length) : OPP.all;
  const partial = OPP.seed ? (OPP.total_rows !== scopeN)
                           : (OPP.total !== OPP.all_groups || OPP.total_rows !== OPP.all);
  document.getElementById("oppCount").textContent = OPP.seed
    ? "快照 " + scopeN + " 条 · 归并后 " + OPP.total + " 家"
    : (partial ? "筛出 " + OPP.total + " 家（" + OPP.total_rows + " 条岗位）"
               : "共 " + OPP.all_groups + " 家公司 · " + OPP.all + " 条岗位");
  document.getElementById("tabOpps").textContent = OPP.all || "";

  document.getElementById("oppNote").innerHTML =
    (OPP.seed ? "<b>这是离线样例页</b>（没有后端），只嵌了 " + ((OPP_SEED.rows || []).length) +
      " 条快照——搜索、筛选、排序都只在这 " + ((OPP_SEED.rows || []).length) +
      " 条里生效，底下那些分布数字是整张表的。<br>" : "") +
    "这张表是从别人的校招信息汇总同步下来的，全表 " + OPP.all + " 条" +
    (OPP.synced_at ? "，最近一次同步 " + esc(OPP.synced_at) : "") + "。" +
    "源表里的字段全都搬过来了：公司名称、行业大类、企业性质、招聘对象、批次、笔试/测评、" +
    "工作地点、截止时间、更新时间、投递方式、官方公告、招聘岗位、备注/提示。" +
    "<br><b>一家公司只占一行</b>：源表把一家公司拆成很多条 —— 中国邮政储蓄银行 " +
    "30 多个「XX省/市分行」、招商银行 50 多个、中信期货 40 多个分公司，一条一行的话" +
    "整页全是同一家公司的名字。所以列表按公司并过了：主行写着「N 个分行 / N 个岗位」，" +
    "第二行是各分行/岗位的短名，<b>点这一行就摊开看每一条</b>（再点一下收起）。" +
    "地点和截止时间取的是这家最全的：地点列合并了全部分行、" +
    "截止时间取「最近的一个还没过期的」——先投这个。" +
    "只有一条的公司就直接铺一条，不套折叠壳。<br>" +
    "<b>表格里的每一个标签都能点</b>——点一下按它筛，再点一下取消；每行最后的 " +
    "⌄ 会把这条记录的全部字段摊开，里面的标签同样能点。" +
    "<br>搜索分<b>包含</b>和<b>不含</b>两块、<b>同时生效</b>，每块都能挂好几条条件" +
    "（右边的 ＋ 加一条、× 删一条）：每条条件先选字段、再填词——「包含」里的词必须都出现，" +
    "「不含」里的词出现任何一个就把这条整条排掉。比如包含「行业=金融业」+ 包含「城市=广州」，" +
    "再挂一条不含「城市=深圳」，三个条件一起算。字段留「全部字段」就是整条记录都搜" +
    "（公司 / 岗位 / 行业 / 城市 / 企业性质 / 招聘对象 / 批次 / 备注 / 免笔试 / 截止时间 / 更新时间）。" +
    "排序除了截止时间 / 更新时间 / 公司名，还多了按行业，右边的 " +
    "↑正序 / ↓反序 可以把任何一条排序规则反过来。" +
    "<br><b>「最近新开」</b>点一下换一档：<b>7 天内 → 3 天内 → 今天 → 关闭</b>，" +
    "只看最近有动静的岗位，开着的时候排序会自动切到「按更新时间」，最新的排最前面；" +
    "公司名右边冒出 <b>今天 / 1天前 / 2天前 / 3天前</b> 的小标，就说明这条最近动过。" +
    "口径是源表的<b>「更新时间」</b>：新开的岗位必然在里面，被重新编辑过的老岗位也会进来。" +
    (OPP.seed ? "" :
      "<br><b>这张源表每天都在更新</b>：本地服务一开着就有人盯着它 —— 超过 " +
      (OPP.ttl_h || 6) + " 小时没同步，服务自己会在后台拉一次飞书，" +
      "不用点开这个标签页，也不用等；想立刻拉最新的，点右边「检查更新」。") +
    "<br>截止时间是表里手写的文本，有的写「招满为止」、有的写具体日期，能认出日期就按日期排，" +
    "认不出的（占大多数）永远沉到最后、也不算逾期。「企业性质」筛选项把源表里" +
    "「民企 / 民营企业 / 私企」这类同义写法并成了一条，展开行里能看到原表的原话。" +
    (OPP.seed ? "<br><b>上面那 60 条是嵌进来的快照</b>，所以这里的搜索/筛选/排序都是在本机对" +
      "这 60 条算的；起本地服务（<code>start.command</code>）就是全表 9000 多条了。" : "");
}
document.getElementById("oppTb").addEventListener("click", e => {
  const det = e.target.closest(".detbtn");
  if(det){
    const k = det.dataset.det;
    OPP_DET[k] = !OPP_DET[k];
    renderOpps();
    return;
  }
  const f = e.target.closest(".cellf, .dtag");
  if(f && f.dataset.k) oppSet(f.dataset.k, f.dataset.v);
});
// —— 搜索条件行：包含 / 不含 两块，每块都能挂多条「字段 + 词」——
// 值一律存在 oppInc / oppExc 里、改一个字就立刻写回数组，只把请求延后 260ms；
// 这样在 A 框打完字马上切到 B 框，A 的输入不会被取消掉的那次定时器吞掉。
function qLive(){
  clearTimeout(oppTimer);
  oppTimer = setTimeout(() => { oppLimit = 60; loadOpps(); }, 260);
}
function qRowSync(row, c, kind, fill){
  row._sel.value = c.f;
  row._sel.classList.toggle("has", !!c.f);
  // 只有重建结构之后才把字写回去；平时（每 10 秒的自动刷新也会走这里）
  // 不碰输入框，否则会把正在打字的人的光标顶到末尾、还可能吞掉尾空格
  if(fill) row._inp.value = c.v;
  const name = OPP_FIELD_NAME[c.f] || "全部字段";
  const all = !c.f;
  row._inp.placeholder = kind === "inc"
    ? (all ? "要出现的词，空格分词（公司 / 岗位 / 行业 / 城市…）" : "在「" + name + "」里要出现的词")
    : (all ? "出现这些词就排除" : "「" + name + "」里出现就排除");
  row._inp.classList.toggle("has", !!c.v);
}
function qRowsBox(kind){
  return document.getElementById(kind === "inc" ? "incRows" : "excRows");
}
function qRowBuild(kind){
  const row = document.createElement("div");
  row.className = "qrow";
  const sel = document.createElement("select");
  sel.className = "qf" + (kind === "exc" ? " excl" : "");
  sel.title = kind === "inc" ? "先选一个字段，再填要出现的词" : "先选一个字段，再填一定不能出现的词";
  OPP_FIELD_OPTS.forEach(o => {
    const op = document.createElement("option");
    op.value = o[0]; op.textContent = o[1];
    sel.appendChild(op);
  });
  const inp = document.createElement("input");
  inp.type = "search";
  const del = document.createElement("button");
  del.type = "button"; del.className = "qdel"; del.textContent = "×";
  del.title = "删掉这条条件";
  // 无障碍 A2：读屏会把 × 念成「乘号」——可达名必须是词，不是符号
  del.setAttribute("aria-label", "删掉这条条件");
  row.appendChild(sel); row.appendChild(inp); row.appendChild(del);
  row._sel = sel; row._inp = inp;      // 存一下，省得回头反复查 DOM
  return row;
}
// 只在「加一条 / 删一条」时重建结构；重建后值从数组回填，光标落到新那一行
function qRowsRender(kind){
  const box = qRowsBox(kind);
  const arr = kind === "inc" ? oppInc : oppExc;
  box.innerHTML = "";
  arr.forEach(c => {
    const row = qRowBuild(kind);
    row._item = c;      // 直接挂到数组里那个对象上 —— 加/删之后不用重排索引，也不会错位
    qRowSync(row, c, kind, true);
    box.appendChild(row);
  });
}
function qRowsSync(){
  [["inc", oppInc], ["exc", oppExc]].forEach(pair => {
    [...qRowsBox(pair[0]).children].forEach(row => {
      if(row._item) qRowSync(row, row._item, pair[0], false);
    });
  });
}
function qRowsAdd(kind){
  (kind === "inc" ? oppInc : oppExc).push({f:"", v:""});
  qRowsRender(kind);
  const last = qRowsBox(kind).lastElementChild;
  if(last) last._inp.focus();
  qLive();
}
function qRowsInit(kind){
  const box = qRowsBox(kind);
  const arr = kind === "inc" ? oppInc : oppExc;
  const hit = (e, tag) => {
    if(e.target.tagName !== tag) return null;
    const row = e.target.closest(".qrow");
    return row && row._item ? {row: row, c: row._item} : null;
  };
  box.addEventListener("input", e => {
    const h = hit(e, "INPUT"); if(!h) return;
    h.c.v = e.target.value.trim().toLowerCase();
    qRowSync(h.row, h.c, kind);
    qLive();
  });
  box.addEventListener("change", e => {
    const h = hit(e, "SELECT"); if(!h) return;
    h.c.f = e.target.value;
    qRowSync(h.row, h.c, kind);
    qLive();
  });
  box.addEventListener("click", e => {
    if(!e.target.classList.contains("qdel")) return;
    const row = e.target.closest(".qrow");
    const i = row && row._item ? arr.indexOf(row._item) : -1;
    if(i >= 0) arr.splice(i, 1);
    if(!arr.length) arr.push({f:"", v:""});   // 最后一条不给删，留着当空框用
    qRowsRender(kind);
    qLive();
  });
}
oppLoad();   // 先恢复上次的条件，再铺输入行 —— 顺序反了恢复值会被空行盖掉
qRowsRender("inc"); qRowsRender("exc");
qRowsInit("inc"); qRowsInit("exc");
document.getElementById("incAdd").onclick = () => qRowsAdd("inc");
document.getElementById("excAdd").onclick = () => qRowsAdd("exc");
document.getElementById("oppSync").onclick = async () => {
  const b = document.getElementById("oppSync");
  if(b.disabled) return;
  b.disabled = true; b.textContent = "同步中…";
  try{
    await fetch("/api/opps-sync", {method:"POST", headers:{"Content-Type":"application/json"},
                                   body: "{}", cache: "no-store"});
    // 后台在拉，页面这边每 1.5 秒问一次好了没（9000 多条要十几页，慢的时候得等一会儿）
    for(let i = 0; i < 80; i++){
      await new Promise(r => setTimeout(r, 1500));
      const r = await fetch("/api/opps?" + oppQuery(), {cache: "no-store"});
      const d = await r.json();
      Object.assign(OPP, d, {loaded: true, bad: !!d.bad});
      renderOpps();
      if(!d.busy) break;
      b.textContent = "同步中… " + (d.all || 0);
    }
  }catch(e){ /* 拉失败就照旧看本地数据，页面已经提示过了 */ }
  b.disabled = false; b.textContent = "检查更新";
};
document.getElementById("oppInd").onchange = e => { oppInd = e.target.value; oppLimit = 60; loadOpps(); };
document.getElementById("oppCity").onchange = e => { oppCity = e.target.value; oppLimit = 60; loadOpps(); };
document.getElementById("oppNat").onchange = e => { oppNat = e.target.value; oppLimit = 60; loadOpps(); };
document.getElementById("oppBatch").onchange = e => { oppBatch = e.target.value; oppLimit = 60; loadOpps(); };
document.getElementById("oppOpen").onclick = () => { oppOpen = !oppOpen; oppLimit = 60; loadOpps(); };
document.getElementById("oppMine").onclick = () => { oppMine = !oppMine; oppLimit = 60; loadOpps(); };
document.getElementById("oppNew").onclick = () => {
  // 转一圈：关 → 7 天内 → 3 天内 → 今天 → 关
  oppNew = OPP_FRESH_STEPS[(OPP_FRESH_STEPS.indexOf(oppNew) + 1) % OPP_FRESH_STEPS.length];
  // 看「最近新开」的时候，按更新时间排最符合直觉，顺手带过去（关掉时不还原，交给她自己调）
  if(oppNew && oppSort !== "up") oppSort = "up";
  oppLimit = 60;
  loadOpps();
};
document.getElementById("oppExam").onclick = () => {
  oppExam = (oppExam === "含免笔试") ? "" : "含免笔试"; oppLimit = 60; loadOpps();
};
document.getElementById("oppRev").onclick = () => { oppRev = !oppRev; oppLimit = 60; loadOpps(); };
[["oppSortDl", "dl"], ["oppSortUp", "up"], ["oppSortCo", "co"], ["oppSortInd", "ind"]].forEach(pair => {
  document.getElementById(pair[0]).onclick = () => { oppSort = pair[1]; oppLimit = 60; loadOpps(); };
});
document.getElementById("oppReset").onclick = () => {
  oppInc = [{f:"", v:""}]; oppExc = [{f:"", v:""}]; oppRev = false;
  oppInd = ""; oppCity = ""; oppNat = ""; oppBatch = ""; oppTarget = ""; oppCo = "";
  oppSort = "dl"; oppOpen = true; oppMine = false; oppExam = ""; oppNew = 0; oppLimit = 60;
  Object.keys(OPP_DET).forEach(k => { delete OPP_DET[k]; });
  qRowsRender("inc"); qRowsRender("exc");
  loadOpps();
};
document.getElementById("oppMoreBtn").onclick = () => { oppLimit += 60; loadOpps(); };

// ---------------- 公司名模糊配对（跟服务端 norm_company 一致）----------------
function normCompany(s){
  s = String(s || "").replace(/[（(][^）)]*[）)]/g, "");
  ["有限责任公司","股份有限公司","有限公司","集团有限公司","集团","控股",
   "信息技术","电子商务","科技","网络","公司"].forEach(x => { s = s.split(x).join(""); });
  return s.replace(/[\s·・\-—_]+/g, "").trim();
}
function sameCompany(a, b){
  a = normCompany(a); b = normCompany(b);
  if(!a || !b) return false;
  if(a === b) return true;
  return a.length >= 2 && b.length >= 2 && (a.includes(b) || b.includes(a));
}
// 严一档的配对：**短名只有 2 个字时，长名必须多出 ≥2 个字才算同一家**（跟服务端
// company_prefix_ok 同口径）。裸子串会把「京东」并进「京东方」—— 两家完全不一样
// （2026-10-09 她亲口纠正），通知表里京东那两条会因此被标成「已投递」（读的是京东方那行）。
// 只用在「通知 / 面试进度 → 哪条投递记录」这类必须真的是同一家的地方；
// 机会库「我投过没」那种软提示仍用上面那个松的（那里宁可多认，不误伤）。
function sameCompanyTight(a, b){
  const x = normCompany(a), y = normCompany(b);
  if(!x || !y) return false;
  const lo = x.length <= y.length ? x : y, hi = x.length <= y.length ? y : x;
  if(lo === hi) return true;
  if(lo.length < 2 || !hi.includes(lo)) return false;
  if(lo.length >= 3) return true;
  return hi.length - lo.length >= 2;
}

// ---------------- 拉数据 / 自动刷新 ----------------
async function pull(showMsg){
  if(OFFLINE){                       // 离线样例：没有 /api/board 可问，如实说一句就够
    if(showMsg) say("这是离线样例文件，没有本地服务可刷新");
    return;
  }
  try{
    const r = await fetch("/api/board", {cache: "no-store"});
    if(!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    const beforeN = APPS.length;
    APPS = d.apps || []; NOTICES = d.notices || []; IVS = d.interview || [];
    if(d.mail_unmatched) DATA.mail_unmatched = d.mail_unmatched;   // 邮箱提示条要用
    if(d.app_parents) APP_PARENT = d.app_parents;      // 「并入」表：别的窗口改的也能跟上
    if(d.meta) Object.assign(META, d.meta);   // 同步状态那几个字段也跟着刷新
    renderApps(); renderNotices();   // 面试进度就在通知表里，renderNotices 一处铺完
    verTick();   // 顺手核对「手里这页是不是旧代码」——轮询里带回的是服务端当前版本
    // 条数变多 = 刚同步进来新记录。光靠表格看不出来（没有行数上限提示），所以主动说一句。
    if(beforeN && APPS.length > beforeN) sayNewApps(APPS.length - beforeN);
    if(showMsg) say("已刷新");
  }catch(e){ if(showMsg) say("连不上本地服务：" + e.message); }
}

// 「刚同步进来 N 条」——把公司名带上，省得她在 138 行里找
function sayNewApps(n){
  const names = (META.apps_changes || []).slice(0, 3)
    .map(c => c && c["公司名称"]).filter(Boolean);
  let s = "同步进来 " + n + " 条投递记录";
  if(names.length) s += "：" + names.slice(0, 2).join("、") + (n > 2 ? " 等" : "");
  say(s);
}

// ---------------- 「你一看就新鲜」 ----------------
// 飞书写入端是实时的（扩展一提交就写进去），但这边 TTL 是 30 分钟 ——
// 她投完立刻切过来看，多半还没有这条，看着就像"没给我记录"。
// 所以：页面加载 / 从别的标签切回来时，只要数据超过 NUDGE_MIN 秒没同步过，就顺手拉一次。
const NUDGE_MIN = 45;               // 秒。45 秒内刚同步过就不重复拉，别让 lark-cli 空转
let nudgeBusy = false;
async function nudgeApps(){
  if(OFFLINE || nudgeBusy) return;
  if((META.apps_source || "") !== "feishu") return;   // 数据源不是飞书，没什么可拉的
  if(META.apps_busy) return;
  const age = Number(META.apps_age_s);
  if(META.apps_age_s !== undefined && age >= 0 && age < NUDGE_MIN) return;
  nudgeBusy = true;
  const before = META.apps_sync_at || "";
  const beforeN = APPS.length;
  try{
    const r = await fetch("/api/apps-sync", {method:"POST", headers:{"Content-Type":"application/json"},
                                             body:"{}", cache:"no-store"});
    const d0 = await r.json().catch(() => ({}));
    if(d0 && d0.ok === false) return;                 // 拉不动就算了，看门狗会兜底
    for(let i = 0; i < 8; i++){                       // 一条页 2~5 秒，等它落地
      await new Promise(res => setTimeout(res, 1200));
      await pull(false);
      if(!META.apps_busy && (META.apps_sync_at || "") !== before) break;
    }
    if(APPS.length === beforeN && !META.apps_bad) return;   // 没变化就别打扰她
  }catch(e){ /* 静默：这是锦上添花，失败了自动刷新的那条路照旧 */ }
  finally{ nudgeBusy = false; }
}

function renderAll(){
  const v = document.getElementById("ver");
  if(v) v.textContent = "v" + (META.version || "");
  const f = document.getElementById("foot");
  if(f) f.innerHTML = "applytracker <b>v" + esc(META.version || "") + "</b> · 页面生成于 " +
    esc(META.at || "") + (OFFLINE ? " · <b>离线样例</b>（没有本地服务，不自动刷新）"
                                 : " · 数据每 10 秒自动刷新一次") +
    " · " + memoChip();
  renderApps(); renderNotices();
}
renderAll();
verTick();      // 加载时也核一次（正常不会触发；服务端刚好在这期间升级了才会）
themeInit();    // 配色切换：把「自动 / 浅色 / 深色」的选中态对上 head 里定下的模式
restoreTab();   // 必须在这里 —— 所有 const 状态都已初始化，见 restoreTab 上的注释
if(!OFFLINE) setInterval(() => pull(false), 10000);
// 打开页面 / 从别的标签切回来就顺手同步一次（数据超过 45 秒没同步才真拉）。
// 这样「刚投完切过来」看到的就是最新的，不必等 30 分钟的 TTL，也不用去点按钮。
if(!OFFLINE){
  nudgeApps();
  document.addEventListener("visibilitychange", () => {
    if(!document.hidden) nudgeApps();
  });
}
</script>
</body>
</html>
"""


def fill_template(data: str, seed: str) -> str:
    """把三份数据填进模板。搜索字段列表只有 OPP_SEARCH_FIELDS 一份，
    从这里注入给页面，省得 HTML 里再抄一遍、抄歪了还没人发现。"""
    return (HTML_TEMPLATE.replace("__DATA__", data)
                        .replace("__OPPS__", seed)
                        .replace("__DARK__", DARK_TOKENS)
                        # 这一页的代码是哪个版本，印在页面里，好让它自己发现「我过期了」（见 verTick）
                        .replace("__APPVER__", APP_VERSION)
                        .replace("__FIELDS__", json.dumps(OPP_SEARCH_FIELDS, ensure_ascii=False)))


def render_html(cfg: Config, seed_opps: bool = False) -> str:
    data = json.dumps(board_payload(cfg), ensure_ascii=False).replace("</", "<\\/")
    if not seed_opps:
        return fill_template(data, "null")
    # 离线文件没法请求 /api/opps，直接把第一屏快照嵌进去
    seed = "null"
    try:
        rows, synced = load_opportunities(cfg)
        if rows:
            apps = load_apps(cfg)
            first = query_opportunities(rows, apps, {"open": ["1"], "sort": ["dl"], "limit": ["60"]})
            first.update({
                "all": len(rows),
                "all_groups": len({r.get("gk") or (r.get("company") or "") for r in rows}),
                "synced_at": synced,
                "source": cfg.opps_source,
                "stats": opp_stats(rows, apps),
                "facets": opp_facets(rows),
            })
            seed = json.dumps(first, ensure_ascii=False).replace("</", "<\\/")
    except Exception:      # 样例文件生成失败不该挡住整个 --report
        seed = "null"
    return fill_template(data, seed)


def write_html(cfg: Config) -> str:
    html = render_html(cfg, seed_opps=True)
    out = os.path.join(cfg.out_dir, "看板.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


# ---------------------------------------------------------------------------
# 本地服务
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore
    server_version = "applytracker/" + APP_VERSION

    def log_message(self, fmt, *args):  # 安静一点，只留一条访问日志
        pass

    # -- 输出 --
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 不回任何跨域放行的响应头：以前一律发 `*`，等于任何网页零点击
        # 就能读走 / 改掉 / 删掉她的记录（ADR-004）。扩展走 host_permissions 本就绕过 CORS，
        # 同源页面也不需要它，回显只会把攻击面放大。
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        """只认本机 Host，防 DNS rebinding 把服务暴露出去。"""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]", "")

    def _origin_ok(self) -> bool:
        """跨站请求防线：只放行「没有 Origin」和「本机 / 扩展」的请求（ADR-004）。

        放行：没有 Origin 的（curl、同源导航、扩展 background 的某些请求）、
              chrome-extension://（用户主动装的扩展，本就在信任边界内）、
              http(s)://127.0.0.1|localhost|::1（看板自身同源）。
        拦截：其余一切 Origin —— 恶意站点用 JS 跨站打 127.0.0.1:8788，零点击就读 / 写 / 删记录。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            p = urlsplit(origin)
        except ValueError:
            return False
        if p.scheme.lower() == "chrome-extension":
            return True
        return (p.hostname or "").lower() in ("127.0.0.1", "localhost", "::1")

    def _guard(self) -> bool:
        """Host + Origin 两道校验都过才算放行；不过就回 403。"""
        if self._host_ok() and self._origin_ok():
            return True
        self._json({"error": "forbidden"}, 403)
        return False

    def _body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            obj = json.loads(raw.decode("utf-8") or "{}")
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    # -- 路由 --
    def do_OPTIONS(self):
        # 预检请求同样过两道校验 —— 以前对任何来源直通 204，等于替跨站页面把门开了。
        if not self._guard():
            return
        self._send(204, b"", "text/plain")

    def do_GET(self):
        if not self._guard():
            return
        path = self.path.split("?")[0]
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")  # 省得浏览器控制台报一条 404
        elif path in ("/", "/index.html"):
            self._send(200, render_html(self.cfg).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/board":
            self._json(board_payload(self.cfg))
        elif path == "/api/opps":
            rows, synced = load_opportunities(self.cfg)
            opps_seed_cache(self.cfg)
            if self.cfg.opps_source == "feishu" and not opps_ok_recently(self.cfg):
                # 没缓存或过期了就在后台拉，这次请求先把手上有的返回，别让页面干等
                sync_opportunities_bg(self.cfg)
            # keep_blank_values：q/qf 是成对的平行数组，字段留「全部字段」时发的是空串，
            # 默认会被 parse_qs 丢掉，那样第 2 条条件就会错配到第 1 条的字段上。
            params = (parse_qs(self.path.split("?", 1)[1], keep_blank_values=True)
                      if "?" in self.path else {})
            apps = load_apps(self.cfg)
            res = query_opportunities(rows, apps, params)
            res.update({
                "synced_at": OPP_STATE.get("synced_at") or synced,
                "all": len(rows),
                # 全表一共多少家公司（归并后的行数）—— 页面写「共 N 家公司 · M 条岗位」
                "all_groups": len({r.get("gk") or (r.get("company") or "") for r in rows}),
                "busy": bool(OPP_STATE.get("busy")),
                "state": str(OPP_STATE.get("msg") or ""),
                "bad": OPP_STATE.get("ok") is False,
                "source": self.cfg.opps_source,
                "ttl_h": round(self.cfg.opp_ttl / 3600.0, 1),   # 页面要拿它写「超过几小时就自动拉」
                "stats": opp_stats(rows, apps),
                "facets": opp_facets_cached(rows, synced),
            })
            self._json(res)
        elif path == "/api/apply-guess":
            # 扩展弹确认卡之前问一句：这个链接以前记过吗？是老记录里的哪家公司？
            q = parse_qs(self.path.split("?", 1)[1], keep_blank_values=True) if "?" in self.path else {}
            res = guess_from_url(self.cfg, (q.get("url") or [""])[0])
            res["ok"] = True
            self._json(res)
        elif path == "/api/health":
            self._json({"ok": True, "app": "applytracker", "version": APP_VERSION})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        path = self.path.split("?")[0]
        body = self._body()

        if path == "/api/companies":
            # 扩展弹卡前把这一页的文字发过来问：这页上出现的是我记过的哪家公司？
            # 拿她自己的名单当字典，比猜页面结构稳。走 POST 是因为页面文字几千字。
            res = match_companies(self.cfg, str(body.get("text", "")))
            res["ok"] = True
            self._json(res)
        elif path == "/api/apply":
            try:
                rec, dup = add_app(
                    self.cfg,
                    company=str(body.get("company", "")),
                    position=str(body.get("position", "")),
                    url=str(body.get("url", "")),
                    status=str(body.get("status", "已投递")),
                    note=str(body.get("note", "")),
                    source=str(body.get("source", "扩展")),
                    applied_at=str(body.get("applied_at", "")),
                )
            except ValueError as e:
                self._json({"ok": False, "error": str(e)}, 400)
                return
            self._json({"ok": True, "duplicate": dup, "record": rec, "total": len(load_apps(self.cfg))})

        elif path == "/api/app-parent":
            # 「把这家的子公司并到主公司下面」—— **只记她点过的那条**，程序不猜。
            # child 传的是页面归一后的公司键；parent 传空串 = 不并了（会盖掉打底表）。
            child = str(body.get("child", "")).strip()
            if not child:
                self._json({"ok": False, "error": "缺少 child"}, 400)
                return
            parents = set_app_parent(self.cfg, child, str(body.get("parent", "")))
            self._json({"ok": True, "child": child,
                        "parent": parents.get(child) or None,
                        "app_parents": parents})

        elif path == "/api/status":
            # 表格里点一下就地改（v0.23.0 起**整行都能改**）：状态 / 备注 / 公司名 / 岗位名 /
            # 投递时间 / 链接都走这一条。参数名 → 列名见 APP_EDIT_FIELDS 白名单。
            fields: Dict[str, str] = {}
            for key, col in APP_EDIT_FIELDS.items():
                if key not in body:
                    continue
                val = str(body.get(key, "") or "")
                fields[col] = norm_applied_at(val) if col == "投递时间" else val.strip()
            if not fields:
                self._json({"ok": False, "error": "没说要改什么"}, 400)
                return
            # 公司名不许清空：CSV 里没有公司名的行读进来会被跳过（那条记录等于凭空消失），
            # 这种「看着像丢了数据」的坑必须在入口挡住，不能靠她自己去猜。
            if "公司名称" in fields and not fields["公司名称"]:
                self._json({"ok": False, "error": "公司名称不能为空"}, 400)
                return
            rec = update_app(self.cfg, str(body.get("id", "")), **fields)
            self._json({"ok": bool(rec), "record": rec} if rec else {"ok": False, "error": "没找到这条记录"}, 200 if rec else 404)

        elif path == "/api/delete":
            ok = delete_app(self.cfg, str(body.get("id", "")))
            self._json({"ok": ok} if ok else {"ok": False, "error": "没找到这条记录"}, 200 if ok else 404)

        elif path == "/api/notice-state":
            # 看板上手动改某条通知的处理状态（存本地，飞书同步不覆盖）
            key = str(body.get("key", ""))
            status = str(body.get("status", "")).strip()
            if not key:
                self._json({"ok": False, "error": "缺少 key"}, 400)
                return
            if status and status not in NOTICE_STATES:
                self._json({"ok": False, "error": "状态只能是：" + " / ".join(NOTICE_STATES)}, 400)
                return
            if not set_notice_state(self.cfg, key, status):
                self._json({"ok": False, "error": "写入失败"}, 500)
                return
            self._json({"ok": True, "key": key, "status": status or None,
                        "overrides": len(load_notice_states(self.cfg))})

        elif path == "/api/notice-type":
            # 邮件认得出公司、认不出类型的那些行，列表里类型留空打「待确认」角标，
            # 点一下挑一个类型 —— 写回 notices.csv（不用本地覆盖表，那会变成两个地方各说一套）
            key = str(body.get("key", ""))
            ntype = str(body.get("ntype", "")).strip()
            if not key:
                self._json({"ok": False, "error": "缺少 key"}, 400)
                return
            if ntype and ntype not in KNOWN_MAIL_TYPES:
                self._json({"ok": False, "error": "类型只能是：" + " / ".join(KNOWN_MAIL_TYPES)}, 400)
                return
            if not set_notice_type(self.cfg, key, ntype):
                self._json({"ok": False, "error": "没找到这条通知，或正在同步（等一下再试）"}, 404)
                return
            self._json({"ok": True, "key": key, "ntype": ntype or None})

        elif path == "/api/notice-add":
            # 手动补一条通知（短信 / 电话来的，邮件收不到）：只存本机 notices_manual.json，
            # 绝不写飞书 —— 反向同步这次不做。
            ok, err, mid = add_manual_notice(
                self.cfg,
                公司名称=str(body.get("公司名称", "")),
                岗位名称=str(body.get("岗位名称", "")),
                类型=str(body.get("类型", "")),
                截止时间=str(body.get("截止时间", "")),
                链接=str(body.get("链接", "")),
                说明=str(body.get("说明", "")),
            )
            if not ok:
                self._json({"ok": False, "error": err}, 400)
                return
            self._json({"ok": True, "id": mid})

        elif path == "/api/notice-del":
            if not del_manual_notice(self.cfg, str(body.get("id", ""))):
                self._json({"ok": False, "error": "没找到这条"}, 404)
                return
            self._json({"ok": True})

        elif path == "/api/interview":
            # 面试进度：点一下节点就把「现在走到哪一环」记下来，存本地（飞书同步不覆盖）
            key = str(body.get("key", "")).strip()
            if not key:
                self._json({"ok": False, "error": "缺少 key"}, 400)
                return
            nodes, stop, cur = body.get("nodes"), body.get("stop"), body.get("cur")
            if nodes is not None and not isinstance(nodes, list):
                self._json({"ok": False, "error": "nodes 得是数组"}, 400)
                return
            if stop is not None and not isinstance(stop, bool):
                self._json({"ok": False, "error": "stop 得是 true / false"}, 400)
                return
            ok = set_interview_progress(
                self.cfg, key, company=str(body.get("company", "")),
                nodes=nodes, cur=(None if cur is None else str(cur)),
                stop=stop, reset=bool(body.get("reset")))
            if not ok:
                self._json({"ok": False, "error": "写入失败（环节不能全空）"}, 500)
                return
            self._json({"ok": True, "key": key, "saved": len(load_interview_progress(self.cfg))})

        elif path == "/api/opps-sync":
            # 页面上点「检查更新」：无视 TTL，后台立刻拉一次；这次请求马上返回，不等它拉完
            if self.cfg.opps_source != "feishu":
                self._json({"ok": False, "error": "数据源不是飞书，没得同步"}, 400)
                return
            if OPP_STATE.get("busy"):
                self._json({"ok": True, "busy": True, "msg": "已经在同步了"})
                return
            sync_opportunities_bg(self.cfg, force=True)
            self._json({"ok": True, "busy": True, "msg": "正在去飞书拉最新的…"})

        elif path == "/api/apps-sync":
            # 投递进度页点「同步飞书进度」：同样无视 TTL，后台拉一次马上返回
            if self.cfg.apps_source != "feishu":
                self._json({"ok": False, "error": "数据源不是飞书，没得同步"}, 400)
                return
            if APP_STATE.get("busy"):
                self._json({"ok": True, "busy": True, "msg": "已经在同步了"})
                return
            sync_apps_bg(self.cfg, force=True)
            self._json({"ok": True, "busy": True, "msg": "正在去飞书拉最新的投递进度…"})

        elif path == "/api/mail-alias":
            # 看板上给「没认出公司」的邮件归一次类：发件人域名 → 公司名。
            # **只记她点过的那条**，下次同域名自动对上（ARCH §4.2）。company 传空 = 撤销归类。
            domain = str(body.get("domain", "")).strip().lower()
            company = str(body.get("company", "")).strip()
            if not domain:
                self._json({"ok": False, "error": "缺少 domain"}, 400)
                return
            if not set_mail_alias(self.cfg, domain, company):
                self._json({"ok": False, "error": "写入失败"}, 500)
                return
            # 归完类顺手重扫一轮（后台），这一页过会儿就自己更新；失败也不影响归类已经存下
            if self.cfg.notices_source == "imap" and self.cfg.mail_enabled and company:
                threading.Thread(target=sync_notices, args=(self.cfg,),
                                 kwargs={"force": True, "quiet": True}, daemon=True).start()
            self._json({"ok": True, "domain": domain, "company": company,
                        "aliases": len(load_mail_aliases(self.cfg))})

        elif path == "/api/mail-setup":
            # 页面直连邮箱（她 2026-10-08：「让直连邮箱简单点，小白也能连」）——
            # 不用开终端：邮箱 + 授权码当场验证，通过就写 config.ini 并立刻收第一轮。
            # ⚠️ 任何响应 / 日志**绝不回显 authcode** —— 它是密码级凭据。
            email = str(body.get("email", "")).strip()
            authcode = str(body.get("authcode", "")).strip()
            host = str(body.get("host", "")).strip()
            port_s = str(body.get("port", "")).strip()
            if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
                self._json({"ok": False, "error": "邮箱地址不像样：要填完整地址，比如 you@qq.com"}, 400)
                return
            if not authcode:
                self._json({"ok": False,
                            "error": "授权码没填。它**不是**登录密码，是邮箱设置里生成的 16 位码"}, 400)
                return
            if not host:
                host, infer_port = mail_infer_server(email)
                if not host:
                    self._json({"ok": False,
                                "error": "认不出这个邮箱的收信服务器。在「服务商」下拉里选「其他邮箱」，"
                                         "手填 IMAP 服务器地址（学校 / 单位邮箱问 IT，一般长 imap.xxx.com 这样）"}, 400)
                    return
            try:
                port = int(port_s) if port_s else 993
            except ValueError:
                port = 993
            ok, err = mail_test_login(host, port, email, authcode)
            if not ok:
                if err.startswith("网络问题") or "timed out" in err.lower() or "getaddrinfo" in err.lower():
                    self._json({"ok": False,
                                "error": "连不上服务器 %s:%s —— 检查手填的服务器地址对不对，或者换个网络再试。" % (host, port)}, 502)
                else:
                    self._json({"ok": False,
                                "error": "登录被拒了。按这个顺序排查：\n"
                                         "① 网页版邮箱设置里先开启 IMAP/SMTP 服务；\n"
                                         "② 要用 16 位授权码，不是登录密码；\n"
                                         "③ 授权码只在生成弹窗显示一次，关掉要重新生成。"}, 401)
                return
            # 写盘：[mail] 全套 —— 跟终端向导 run_mail_wizard() 同一份键。只写 5 个会留半套
            # 配置：「断开再连回来」时 folder / days_back / only_unseen / ttl 会悄悄退成默认值，
            # 跟她上次连的设置不一样，谁也不告诉她（2026-10-08 复盘）。
            cpath = self.cfg.path or "config.ini"
            c = self.cfg
            mail_config_write(cpath, {
                "source": "auto", "host": host, "port": str(port),
                "user": email, "authcode": authcode,
                "folder": c.mail_folder or "INBOX",
                "days_back": str(c.mail_days_back or 30),
                "only_unseen": "true" if c.mail_only_unseen else "false",
                "ttl": str(c.mail_ttl or 900),
            })
            # [notices] source=auto（ADR-001「填了就该生效」；auto + 已配邮箱 = 直连生效）。
            # ⚠️ 只在「一个数据源都没接」时才动这一笔 —— 这个入口本来只会出现在这种人手里，
            # 飞书用户的 source=feishu 不许被碰（别人正用着的表，不该因为连了个备用邮箱被改走）。
            switched = c.notices_source == "off"
            if switched:
                ini_patch(cpath, [("notices", "source", "auto")])
            # 内存里的 cfg 同步改掉，这一进程马上就是直连形态（不用等重启）
            c.mail_host, c.mail_port = host, port
            c.mail_user, c.mail_authcode = email, authcode
            c.mail_source = "auto"
            c.mail_configured = True
            c.mail_enabled = True
            if switched:
                c.notices_source = "imap"
                if not c.notices_csv:
                    c.notices_csv = os.path.join(c.out_dir, "notices.csv")
            # 照 /api/mail-sync 的方式起 force 后台同步，第一轮邮件马上就开始收
            threading.Thread(target=sync_notices, args=(c,),
                             kwargs={"force": True, "quiet": True}, daemon=True).start()
            # 把真相一起回给前端：mode = 写盘后重新算出的数据源，switched = 是否顺手切过去了。
            # 页面靠这两个字段说人话（连上 ≠ 已启用），不再一律弹「正在收第一轮邮件」骗她。
            self._json({"ok": True, "host": host, "user": email,
                        "mode": c.notices_source, "switched": switched})

        elif path == "/api/mail-sync":
            # 邮箱通知页点「收一次邮件」：无视 TTL 后台拉一轮，这次请求马上返回
            if self.cfg.notices_source != "imap":
                self._json({"ok": False, "error": "当前不是邮箱直连，没得同步"}, 400)
                return
            if SYNC_LOCK.locked():
                self._json({"ok": True, "busy": True, "msg": "正在收…"})
                return
            threading.Thread(target=sync_notices, args=(self.cfg,),
                             kwargs={"force": True, "quiet": True}, daemon=True).start()
            self._json({"ok": True, "busy": True, "msg": "正在连邮箱收邮件…"})

        elif path == "/api/mail-source":
            # 「改用直连收信 / 切回飞书表」的把手 —— 由她自己点，不替她猜（2026-10-08 复盘：
            # 连完邮箱面板没反应，就是因为她既没说「要不要改用直连」，我们也没敢动她的飞书源）。
            want = str(body.get("source", "")).strip().lower()
            if want not in ("imap", "feishu"):
                self._json({"ok": False, "error": "source 只能是 imap 或 feishu"}, 400)
                return
            c = self.cfg
            if want == "imap" and not c.mail_configured:
                self._json({"ok": False, "error": "还没连过邮箱，先点「连接我的邮箱」"}, 400)
                return
            if want == "feishu" and not (c.feishu_base and c.feishu_table):
                self._json({"ok": False,
                            "error": "飞书表没配好（缺 base_token / table_id），切不过去"}, 400)
                return
            cpath = c.path or "config.ini"
            ini_patch(cpath, [("notices", "source", want)])
            # 内存 cfg 跟上，这一进程立刻换源（不用等重启）
            c.notices_source = want
            if want in ("imap", "feishu") and not c.notices_csv:
                # 跟 Config.__init__ 同一口径：显式配了 csv 就用它，否则落 data/notices.csv 当本地缓存
                explicit = _ini_read(cpath, "notices", "csv", "")
                c.notices_csv = (explicit if os.path.isabs(explicit)
                                 else (os.path.join(c.base, explicit) if explicit
                                       else os.path.join(c.out_dir, "notices.csv")))
            msg = "已改用邮箱直连，正在收第一轮邮件…" if want == "imap" else "已切回飞书表"
            # 换完源立刻起一轮 force 后台同步，页面过会儿自己更新
            threading.Thread(target=sync_notices, args=(c,),
                             kwargs={"force": True, "quiet": True}, daemon=True).start()
            self._json({"ok": True, "mode": want, "msg": msg})

        elif path == "/api/mail-off":
            # 「断开直连」：把 [mail] source 写成 off（**凭据和邮箱留着** —— 下次一点就连回来，
            # 不用再去找一遍授权码）。看板状态不能因为断开变成「imap 却没凭据」的死状态，
            # 所以顺手把 [notices] source 落回它本来该去的地方：有飞书表走飞书，否则 off。
            c = self.cfg
            cpath = c.path or "config.ini"
            fallback = "feishu" if (c.feishu_base and c.feishu_table) else "off"
            changes = [("mail", "source", "off")]
            if c.notices_source == "imap":
                changes.append(("notices", "source", fallback))
            ini_patch(cpath, changes)
            c.mail_source = "off"
            c.mail_enabled = False
            if c.notices_source == "imap":
                c.notices_source = fallback
                if fallback == "off":
                    c.notices_csv = ""
            self._json({"ok": True, "mode": c.notices_source})

        elif path == "/api/mail-on":
            # 「断开」之后的**一键回连**：授权码本来就躺在 config.ini 的 [mail] 里，
            # 再让她去 QQ 邮箱重新生成一遍纯属折磨（她定的规矩：能不给她的地方就别让她手打）。
            # 只把 [mail] source 写回 auto —— 不会去动 [notices] source，面板要读哪个源
            # 仍然由「改用直连收信 / 切回飞书表」那两个按钮决定（飞书用户不会被偷偷换源）。
            c = self.cfg
            if not c.mail_configured:
                self._json({"ok": False, "error": "没存过邮箱凭据，先点「连接我的邮箱」"}, 400)
                return
            cpath = c.path or "config.ini"
            ini_patch(cpath, [("mail", "source", "auto")])
            c.mail_source = "auto"
            c.mail_enabled = True
            self._json({"ok": True, "mode": c.notices_source})

        else:
            self._json({"error": "not found"}, 404)


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# 每日备份（ADR-006）
# ---------------------------------------------------------------------------
# 投递记录是她三周海投的唯一凭证，误删 / 误覆盖就不可再生（不像机会库能重拉）。
# 每次启动给这几个文件拍一份「当日快照」，留最近 14 天。备份失败**绝不**拦住看板启动。
BACKUP_KEEP_DAYS = 14


def backup_dir(cfg: Config) -> str:
    return os.path.join(cfg.out_dir, "backups")


def _backup_sources(cfg: Config) -> List[Tuple[str, str]]:
    """要备份的 (源文件, 备份名)。只挑不可再生的，能重拉的（机会库 / 通知 CSV）不备份。"""
    return [
        (cfg.app_csv, "applications.csv"),
        (cfg.pending_csv, "待投递.csv"),
        (os.path.join(cfg.out_dir, "app_parents.json"), "app_parents.json"),
        (os.path.join(cfg.out_dir, "app_renames.json"), "app_renames.json"),
        (os.path.join(cfg.out_dir, "notice_states.json"), "notice_states.json"),
        (os.path.join(cfg.out_dir, "notices_manual.json"), "notices_manual.json"),
        (os.path.join(cfg.out_dir, "interview_progress.json"), "interview_progress.json"),
    ]


def _prune_backups(dst: str, name: str, keep: int) -> None:
    """同一个文件只留最近 keep 份（按日期文件名排序，ISO 日期排起来就是时间序）。"""
    prefix = name + "."
    try:
        entries = sorted(e for e in os.listdir(dst) if e.startswith(prefix))
    except OSError:
        return
    for old in entries[:-keep]:
        try:
            os.remove(os.path.join(dst, old))
        except OSError:
            pass


def daily_backup(cfg: Config, today: str = "") -> int:
    """给 data/ 里不可再生的文件拍一份当日快照到 data/backups/，返回新写了几个。

    幂等：当日快照已存在就跳过（连跑两次只产生一份）。任何异常都只打印一行中文提示后返回 0
    —— 备份不能让看板起不来。today 可注入假日期（自检用）。
    """
    try:
        dst = backup_dir(cfg)
        os.makedirs(dst, exist_ok=True)
        day = today or datetime.now().strftime("%Y-%m-%d")
        written = 0
        for src, name in _backup_sources(cfg):
            if not os.path.exists(src):
                continue
            target = os.path.join(dst, "%s.%s" % (name, day))
            if not os.path.exists(target):
                shutil.copy2(src, target)
                written += 1
            _prune_backups(dst, name, BACKUP_KEEP_DAYS)
        if written:
            print("· 已备份 %d 个文件到 %s" % (written, dst))
        return written
    except OSError as e:
        print("· 备份没做成（不影响使用）：%s" % e)
        return 0


def pick_port(host: str, preferred: int, last: int = PORT_LAST):
    """在 [preferred, last] 里挑第一个能起来的端口，返回**已经绑好**的 (httpd, port)。

    为什么是「直接建 server」而不是「先探一下再 bind」：探完关掉、再 bind 中间有条缝，
    别的进程正好挤进来就又炸了。这里每试一个端口就真建一次，建成功即占用，没有缝。

    **不区分「是不是显式传 --port」**：不论是 start.command 明确指的，还是从配置读来的
    默认值，被占就一律往上让。少一个分支，两条启动路径（start.command / 开机自启）自然都对。

    preferred 已经超出区间（比如 --port 9000）时**只在它自己身上试一次**，不偷偷挪到 9001
    —— 人既然手点了那个端口，就按那个端口办，别擅自漂。区间内全占满 → 抛 OSError（中文），
    由调用方翻成给人看的提示。
    """
    start = max(1, int(preferred))
    stop = max(start, int(last))
    last_err: Optional[BaseException] = None
    for p in range(start, stop + 1):
        try:
            return ThreadingServer((host, p), Handler), p
        except OSError as e:
            last_err = e
    if start == stop:
        raise OSError("端口 %d 起不来（%s）—— 换一个，比如 --port 9000" % (start, last_err))
    raise OSError("%d–%d 这几个端口都被占着了（最后一个：%s）—— 换一个，比如 --port 9000"
                  % (start, stop, last_err))


def serve(cfg: Config, port: int = 0, open_browser: bool = True) -> int:
    preferred = int(port or cfg.port or DEFAULT_PORT)
    Handler.cfg = cfg
    daily_backup(cfg)          # 启动先留一份当日快照；失败也只提示，不阻断（ADR-006）
    if cfg.apps_source == "feishu":
        # 先把上次同步时间读回来：重启不等 TTL 不白拉，页面也能立刻写出「上次几点同步的」
        apps_seed_state(cfg)
    if cfg.opps_source == "feishu" or cfg.notices_source == "imap":
        # 服务活着就一直盯着机会库 / 邮箱通知的 TTL，不依赖页面被打开
        threading.Thread(target=sync_watchdog, args=(cfg,), daemon=True).start()
    if cfg.notices_source == "feishu":
        # 先把通知拉下来，免得第一眼打开看板是空的（lark-cli 一次 2~5 秒）
        print("正在从飞书同步邮箱通知…")
        sys.stdout.flush()
        sync_notices(cfg)
    elif cfg.notices_source == "imap":
        # 邮箱直连：认一下上次同步时间，别重启就白连一次；第一眼打开还是空的就顺手拉一轮
        notices_seed_state(cfg)
        if not sync_ok_recently(cfg):
            print("正在连邮箱同步通知（%s）…" % cfg.mail_user)
            sys.stdout.flush()
            sync_notices(cfg)
    try:
        httpd, port = pick_port("127.0.0.1", preferred)
    except OSError as e:
        print("✗ 端口起不来：%s" % e)
        print("  如果已经有一个看板在跑，直接开浏览器就行；要换端口：--port 9000")
        return 1
    if port != preferred:
        # 期望的端口和实际起的不一样时必须说一句 —— 否则人照着 8788 打不开，还以为没起来
        print("[提示] 端口 %d 被占，看板改跑在 %d → http://127.0.0.1:%d/（浏览器扩展会自动找到它）"
              % (preferred, port, port))
    url = f"http://127.0.0.1:{port}/"
    if cfg.notices_source == "feishu":
        notices = "飞书多维表格（%s）" % str(SYNC_STATE.get("msg") or "")
    elif cfg.notices_source == "imap":
        notices = "邮箱直连 %s（%s）" % (cfg.mail_user, str(SYNC_STATE.get("msg") or ""))
    else:
        notices = cfg.notices_csv or "（未配置）"
    print("=" * 58)
    print(f"秋招看板已启动 → {url}")
    print(f"投递记录： {cfg.app_csv}")
    print(f"邮箱通知： {notices}")
    print("按 Ctrl+C 退出")
    print("=" * 58)
    sys.stdout.flush()
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def self_test() -> int:
    import tempfile

    ok = True

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        print(f"  {'✓' if cond else '✗'} {name}{('  ' + extra) if extra and not cond else ''}")
        ok = ok and cond

    # 自检偶尔要看自己的源码：比如「这条路由到底挂在 GET 还是 POST 上」
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as _f:
            src = _f.read()
    except OSError:
        src = ""

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(None, base=tmp)
        cfg.app_csv = os.path.join(tmp, "applications.csv")

        check("空 CSV 不报错", load_apps(cfg) == [])

        rec, dup = add_app(cfg, "云溪智能", "内容运营", "https://hr.yunxi-tech.com/job/1")
        check("新增一条", not dup and rec["id"])
        check("落盘后能读回", len(load_apps(cfg)) == 1)

        _, dup2 = add_app(cfg, "云溪智能", "内容运营", "https://hr.yunxi-tech.com/job/1")
        check("10 分钟内重复提交判重", dup2 and len(load_apps(cfg)) == 1)

        _, dup3 = add_app(cfg, "星桥科技", "产品运营", "https://join.qq.com/x")
        check("换公司不算重复", not dup3 and len(load_apps(cfg)) == 2)

        check("同公司换岗位算新投递",
              not add_app(cfg, "云溪智能", "市场策划", "")[1] and len(load_apps(cfg)) == 3)

        check("状态别名归一（投递成功→已投递）", norm_status("投递成功") == "已投递")
        check("状态别名归一（二面→面试）", norm_status("二面") == "面试")
        check("乱写的状态兜到已投递", norm_status("随便写") == "已投递")

        r = update_app(cfg, rec["id"], 状态="面试")
        check("更新状态生效", r is not None and r["状态"] == "面试"
              and load_apps(cfg)[0]["状态"] == "面试")
        check("状态非法值被归一", update_app(cfg, rec["id"], 状态="xxx")["状态"] == "已投递")
        check("找不到 id 返回 None", update_app(cfg, "nope", 状态="面试") is None)
        check("删除生效", delete_app(cfg, rec["id"]) and len(load_apps(cfg)) == 2)

        # 备注要能在看板上点着改：页面的 editNote() 打的就是 /api/status 的 note 分支，
        # 那个分支落到 update_app 的「备注」字段上。这条链断了她会以为"改了没反应"。
        _n, _ = add_app(cfg, "云溪智能", "管理向管培生", "https://yunxi.example.com/job/1")
        check("备注能改", (update_app(cfg, _n["id"], 备注="内推码 R7K205") or {}).get("备注") == "内推码 R7K205")
        check("备注能清空", (update_app(cfg, _n["id"], 备注="") or {}).get("备注") == "")
        _got = [x for x in load_apps(cfg) if x["id"] == _n["id"]]
        check("改备注不会顺手把状态冲掉", bool(_got) and _got[0]["状态"] == "已投递")

        # v0.23.0：整行都能改（公司名 / 岗位名 / 投递时间 / 链接），都走 /api/status 白名单。
        # 故意把它改成**另一家公司**（扩展把公司名认错了，她在看板上改回来的真实场景）：
        # 这种改名连「宽松比对」都救不回来，只有曾用名备忘能救。
        update_app(cfg, _n["id"], 公司名称="宏图地产", 岗位名称="管理培训生",
                   投递时间="2026-09-14 00:00", 网页链接="https://yunxi.example.com/job/9")
        _got2 = [x for x in load_apps(cfg) if x["id"] == _n["id"]][0]
        check("改公司名 / 岗位名 / 投递时间 / 链接都生效（改完重新读盘还是新的）",
              _got2["公司名称"] == "宏图地产" and _got2["岗位名称"] == "管理培训生" and
              _got2["投递时间"] == "2026-09-14 00:00" and
              _got2["网页链接"] == "https://yunxi.example.com/job/9")
        _rnm = load_app_renames(cfg)
        check("改了公司名就把曾用名记下来（同步那边要靠它认出是同一条）",
              _n["id"] in _rnm and "云溪智能" in (_rnm[_n["id"]].get("公司") or []) and
              "管理向管培生" in (_rnm[_n["id"]].get("岗位") or []))
        # 这才是这个备忘存在的理由：飞书那条还挂着**旧名字**，同步不许再插一条新的进来
        _feishu_row = {"公司名称": "云溪智能", "岗位名称": "管理向管培生", "状态": "已投递",
                       "投递时间": "", "网页链接": "", "备注": "", "_原状态": "已投递"}
        _n0 = len(load_apps(cfg))
        _m2, _s2 = merge_apps(load_apps(cfg), [_feishu_row], renames=_rnm)
        check("同步按曾用名认人 —— 改过名的记录不会被当成新记录再插一条",
              _s2["added"] == 0 and len(_m2) == _n0)
        check("不传曾用名时确实会多插一条（证明上面那条靠的是这个备忘，不是碰巧）",
              merge_apps(load_apps(cfg), [_feishu_row])[1]["added"] == 1 and
              len(merge_apps(load_apps(cfg), [_feishu_row])[0]) == _n0 + 1)
        check("删掉记录时顺手清掉它的曾用名（备忘文件不会越攒越胖）",
              delete_app(cfg, _n["id"]) and _n["id"] not in load_app_renames(cfg))
        # 投递时间归一：页面上的时间选择器给的是 2026-10-04T13:34；只写日期的补 00:00；
        # 她手写的「10 月初」这种原样留着 —— 不许把她的字吃掉
        check("投递时间归一：T 分隔符 / 只写日期 / 认不出的原样留",
              norm_applied_at("2026-10-04T13:34") == "2026-10-04 13:34" and
              norm_applied_at("2026-10-04") == "2026-10-04 00:00" and
              norm_applied_at("10 月初") == "10 月初" and norm_applied_at("") == "")

        # ---- 发行版加固（ADR-004/005/006）----
        # 写入一致性：以前 load → 改内存 → 全量覆盖且无锁，10 并发只进 3 条。
        with tempfile.TemporaryDirectory() as tmpc:
            ccfg = Config(None, base=tmpc)
            ccfg.app_csv = os.path.join(tmpc, "applications.csv")

            def _one(i):
                add_app(ccfg, "并发公司%d" % i, "岗位", "https://x/%d" % i)

            ths = [threading.Thread(target=_one, args=(i,)) for i in range(50)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            check("写入加锁：50 并发 add_app 一条不丢（行数 = 50）",
                  len(load_apps(ccfg)) == 50, "n=%d" % len(load_apps(ccfg)))

        # 保列：她自己在 Excel 里加的列（含列序）不许被静默抹掉。
        with tempfile.TemporaryDirectory() as tmpk:
            kcfg = Config(None, base=tmpk)
            kcfg.app_csv = os.path.join(tmpk, "applications.csv")
            with open(kcfg.app_csv, "w", newline="", encoding="utf-8-sig") as f:
                f.write("id,公司名称,岗位名称,状态,飞书状态,投递时间,网页链接,来源,备注,更新时间,内推人\n")
                f.write("aa11,某某公司,运营,已投递,,2026-09-20 10:00,,扩展,,2026-09-20 10:00,王学姐\n")
            check("保列：读回来的行带着自定义列「内推人」",
                  (load_apps(kcfg)[0].get("内推人") or "") == "王学姐")
            update_app(kcfg, "aa11", 状态="面试")
            with open(kcfg.app_csv, encoding="utf-8-sig") as f:
                hdr = f.readline().strip()
            check("保列：update 之后自定义列还在、列序也没被挪动",
                  hdr == "id,公司名称,岗位名称,状态,飞书状态,投递时间,网页链接,来源,备注,更新时间,内推人",
                  hdr)
            _kr = load_apps(kcfg)[0]
            check("保列：改完状态，自定义列的值也没丢",
                  _kr.get("内推人") == "王学姐" and _kr["状态"] == "面试")
            check("保列：新记录也写进这张带自定义列的表（新行该列留空）",
                  add_app(kcfg, "另一家", "测试", "")[0]["公司名称"] == "另一家" and
                  load_apps(kcfg)[1].get("内推人") == "")

        check("写入的唯一入口 mutate_apps + APP_FILE_LOCK 都在（读—改—写全在锁里）",
              "def mutate_apps" in src and "APP_FILE_LOCK = threading.RLock()" in src and
              "with APP_FILE_LOCK:" in src)

        # Origin 校验：恶意网页要拦，扩展 / 本机 / 无 Origin 放行（ADR-004）。
        class _FakeReq:
            def __init__(self, origin):
                self.headers = {} if origin is None else {"Origin": origin}
            _origin_ok = Handler._origin_ok
        check("Origin 校验：无 Origin（curl / 同源导航）放行", _FakeReq(None)._origin_ok() is True)
        check("Origin 校验：chrome-extension:// 放行",
              _FakeReq("chrome-extension://abcdefghijklmnop")._origin_ok() is True)
        check("Origin 校验：http://127.0.0.1:8788 与 http://localhost:8788 放行",
              _FakeReq("http://127.0.0.1:8788")._origin_ok() is True and
              _FakeReq("http://localhost:8788")._origin_ok() is True)
        check("Origin 校验：别的站一律拒（读不到也写不动）",
              _FakeReq("https://evil.com")._origin_ok() is False and
              _FakeReq("http://127.0.0.1.evil.com")._origin_ok() is False and
              _FakeReq("null")._origin_ok() is False)
        _acao = "Access-Control-" + "Allow-Origin"     # 拆开写，免得这条断言自己命中自己
        _acah = "Access-Control-" + "Allow-Headers"
        check("所有响应都不再回跨域放行头（按字符串核一遍源码）",
              _acao not in src and _acah not in src)

        # 每日备份：当日只拍一份、内容跟源文件字节一致、只留 14 天、失败不阻断（ADR-006）。
        with tempfile.TemporaryDirectory() as tmpb:
            bcfg = Config(None, base=tmpb)
            bcfg.out_dir = tmpb
            bcfg.app_csv = os.path.join(tmpb, "applications.csv")
            with open(bcfg.app_csv, "w", encoding="utf-8-sig") as f:
                f.write("id,公司名称\nx1,备份公司\n")
            n1 = daily_backup(bcfg, today="2026-10-07")
            n2 = daily_backup(bcfg, today="2026-10-07")     # 当日已有 → 跳过（幂等）
            bpath = os.path.join(tmpb, "backups", "applications.csv.2026-10-07")
            check("每日备份：当日快照落 data/backups/，连跑两次只有一份",
                  n1 == 1 and n2 == 0 and os.path.exists(bpath))
            with open(bpath, "rb") as _bf, open(bcfg.app_csv, "rb") as _sf:
                check("每日备份：快照内容与源文件字节一致", _bf.read() == _sf.read())
            for _d in range(1, 20):                          # 造 19 天历史
                daily_backup(bcfg, today="2026-09-%02d" % _d)
            _kept = [e for e in os.listdir(os.path.join(tmpb, "backups"))
                     if e.startswith("applications.csv.")]
            check("每日备份：只留最近 14 天", len(_kept) == 14, "kept=%d" % len(_kept))
            _blocker = os.path.join(tmpb, "blocker")
            with open(_blocker, "w") as _f:
                _f.write("x")
            _bad = Config(None, base=tmpb)
            _bad.out_dir = _blocker          # 拿一个文件当目录 → 建 backups/ 必失败
            _bad.app_csv = os.path.join(_blocker, "applications.csv")
            check("每日备份：备份失败不抛异常、只提示（不阻断启动）",
                  daily_backup(_bad, today="2026-10-07") == 0)

        # 公司名归一与配对
        check("归一：云溪智能（杭州）网络有限公司 → 云溪智能", norm_company("云溪智能（杭州）网络有限公司") == "云溪智能")
        check("配对：云溪智能 vs 云溪智能（杭州）网络有限公司", same_company("云溪智能", "云溪智能（杭州）网络有限公司"))
        check("配对：星桥科技 vs 星桥科技（中国）有限公司", same_company("星桥科技", "星桥科技（中国）有限公司"))
        check("不配对：云溪智能 vs 星桥科技", not same_company("云溪智能", "星桥科技"))
        check("不配对：空名", not same_company("", "云溪智能"))

        # 通知 CSV + 关联
        npath = os.path.join(tmp, "notices.csv")
        with open(npath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["公司名称", "岗位名称", "通知类型", "通知截止时间", "邮件链接",
                        "邮件主题", "邮件收到时间", "备注"])
            w.writerow(["云溪智能（杭州）网络有限公司", "内容运营", "测评、笔试", "2026-09-30 23:59",
                        "https://x/1", "测评邀请", "2026-09-26 09:00", ""])
            w.writerow(["云溪智能（杭州）网络有限公司", "内容运营", "测评、笔试", "2026-09-30 23:59",
                        "https://x/1", "测评邀请", "2026-09-26 09:00", ""])
            w.writerow(["星桥科技", "", "AI面", "2026-10-02 18:00", "https://x/2", "AI面试邀请",
                        "2026-09-25 10:00", ""])
        cfg.notices_csv = npath
        notices = load_notices(cfg)
        check("通知 CSV 去重 + 类型拆分", len(notices) == 2 and notices[0]["通知类型"] == ["测评", "笔试"])

        # ---- 「同公司归纳」的组键（她 2026-10-08 拍板：统计到面板时同公司归纳到一起，可展开）----
        # 专用的归纳夹具：同公司多条 + 空公司名一条。不动上面那份（后面的关联断言按它算的数）。
        gpath = os.path.join(tmp, "notices_gkey.csv")
        with open(gpath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["公司名称", "岗位名称", "通知类型", "通知截止时间", "邮件链接",
                        "邮件主题", "邮件收到时间", "备注"])
            w.writerow(["示例甲科技", "后端", "面试", "2026-11-01", "https://x/g1", "一面通知",
                        "2026-10-01", ""])
            w.writerow(["示例甲科技", "后端", "笔试", "2026-11-02", "https://x/g2", "笔试通知",
                        "2026-10-01", ""])
            w.writerow(["示例乙银行", "", "测评", "2026-11-03", "https://x/g3", "测评邀请",
                        "2026-10-02", ""])
            w.writerow(["", "", "面试", "", "https://x/g4", "缺公司名的面试通知",
                        "2026-10-03", ""])
        keep_gcsv, cfg.notices_csv = cfg.notices_csv, gpath
        try:
            grows = load_notices(cfg)
            check("通知归纳：每条都带 _gkey（内存派生、不落盘），同公司**精确**同组 —— 甲科技两条进同一组",
                  len(grows) == 4 and all(r.get("_gkey") for r in grows) and
                  len([r for r in grows if r["_gkey"] == "示例甲科技"]) == 2)
            check("通知归纳：空公司名归「（未知公司）」组，不许出现空串键"
                  "（空串会把所有没名字的混成一坨，页面上还显示不出组名）",
                  any(r["_gkey"] == "（未知公司）" for r in grows) and
                  "" not in set(r["_gkey"] for r in grows))
            gpl = board_payload(cfg)
            check("通知归纳：board_payload 双口径 —— notice_companies（组数）与 notice_rows（条数）并列，"
                  "组数按 _gkey 现算（3 家：甲 / 乙 / 未知）",
                  gpl["notice_rows"] == 4 and gpl["notice_companies"] == 3)
        finally:
            cfg.notices_csv = keep_gcsv

        apps = load_apps(cfg)
        attach_notices(apps, notices)
        wang = [a for a in apps if a["公司名称"] == "云溪智能"][0]
        check("投递记录挂上关联通知", wang["关联"]["count"] == 1 and wang["关联"]["types"] == ["测评", "笔试"],
              str(wang["关联"]))
        check("最近截止取最早那条", wang["关联"]["next"] == "2026-09-30 23:59")

        # 关联列的公司比对要严一档（2026-10-09 她贴截图纠正：京东方 ≠ 京东）
        check("严比对：京东 / 京东方 是两家，不许互相关联（短名 2 字 + 长名只多出 1 字 = 撞名）",
              not same_company_tight("京东", "京东方") and not same_company_tight("京东方", "京东"))
        check("严比对：泰康集团 ↔ 泰康人寿 / 泰康保险集团 照旧算同一家（长名多出 ≥2 字）",
              same_company_tight("泰康集团", "泰康人寿") and same_company_tight("泰康集团", "泰康保险集团"))
        check("严比对：建发集团 / 建发股份、达能 / 达能中国、雀巢GCR / 雀巢 都算同一家",
              same_company_tight("建发集团", "建发股份") and same_company_tight("达能", "达能中国")
              and same_company_tight("雀巢GCR", "雀巢"))
        check("严比对：分行照旧认得上（长河银行 ↔ 长河银行杭州分行）",
              same_company_tight("长河银行杭州分行", "长河银行"))
        check("严比对：共用件 same_company 一个字没动（网页 + Edge 扩展还在用它）——"
              "它仍会把京东并进京东方，那是它的老行为，改它要连带跑扩展回归",
              same_company("京东", "京东方") and not same_company_tight("京东", "京东方"))
        _jd = [{"公司名称": "京东方"}]
        attach_notices(_jd, [{"公司名称": "京东", "通知类型": ["测评"],
                              "通知截止时间": "2026-09-13 08:02"}])
        check("端到端：京东的通知不许挂到「京东方」那行上", _jd[0]["关联"]["count"] == 0,
              str(_jd[0]["关联"]))
        _tk = [{"公司名称": "泰康集团"}]
        attach_notices(_tk, [{"公司名称": "泰康人寿", "通知类型": ["面试"],
                              "通知截止时间": "2026-10-20 18:00"}])
        check("端到端：泰康人寿的通知照旧挂在「泰康集团」那行上",
              _tk[0]["关联"]["count"] == 1 and _tk[0]["关联"]["types"] == ["面试"],
              str(_tk[0]["关联"]))

        # ---- 从飞书表格导入历史投递记录 ----
        keep_app_csv, keep_pending = cfg.app_csv, cfg.pending_csv
        cfg.app_csv = os.path.join(tmp, "imp_apps.csv")
        cfg.pending_csv = os.path.join(tmp, "imp_pending.csv")
        try:
            icols = ["公司名称", "岗位名称", "最新状态", "投递时间", "投递链接", "备注",
                     "内推码", "地点", "AI面", "一面"]
            fake = {"ok": True, "data": {"fields": icols, "data": [
                # 常规一条：数组状态 + ISO 时间 + markdown 链接 + 内推码 + 手写轮次日期
                ["星桥科技", "电商运营", ["已AI面"], ["2026-09-04T00:00:00.000+08:00"],
                 "[星桥科技校招](https://careers.xingqiao-tech.com/x)", ["已投递：1/2"], ["R7K205"], ["江苏"],
                 ["9月12日 23:59"], None],
                # 轮次日期是 ISO，要缩成日期
                ["长风物流", "电商运营管培生", ["待一面"], ["2026-09-09T00:00:00.000+08:00"],
                 "[长风物流](https://x.com/1)", None, None, None, None, ["2026-09-20T00:00:00.000+08:00"]],
                # 还没投的，不该混进投递进度
                ["云溪银行", "管理培训生", ["未投递"], None, "[云溪银行](https://x.com/2)",
                 None, None, ["杭州"], None, None],
                # 链接那一列被写成了岗位名（文本列里什么都可能）
                ["某公司", "项目管理", ["已投递"], ["2026-09-28T00:00:00.000+08:00"],
                 "项目管理", None, None, None, None, None],
                # 公司名空着，但从链接标题能看出是谁
                ["", "外协工程师", ["已挂"], ["2026-09-08T00:00:00.000+08:00"],
                 "[万象商贸（北京）有限公司](https://hr.wanxiang-trade.com/x)", None, None, None, None, None],
                # 整行空占位
                [None, None, None, None, None, None, None, None, None, None],
            ]}}
            parsed = parse_feishu_apps(fake)
            check("飞书导入：空行跳过（6 行 → 5 条）", len(parsed) == 5, str(len(parsed)))
            applied_i, pending_i = split_pending(parsed)
            check("飞书导入：「未投递」分流到待投清单，不算投递过",
                  len(pending_i) == 1 and pending_i[0]["公司名称"] == "云溪银行")
            midea = [r for r in applied_i if r["公司名称"] == "星桥科技"][0]
            check("飞书导入：已AI面 → 面试", midea["状态"] == "面试")
            check("飞书导入：ISO 时间 → YYYY-MM-DD HH:MM", midea["投递时间"] == "2026-09-04 00:00")
            check("飞书导入：markdown 链接只取 URL", midea["网页链接"] == "https://careers.xingqiao-tech.com/x")
            check("飞书导入：备注拼上原备注 + 内推码",
                  "已投递：1/2" in midea["备注"] and "内推码 R7K205" in midea["备注"], midea["备注"])
            check("飞书导入：手写的轮次日期原样留着", "AI面 9月12日 23:59" in midea["备注"], midea["备注"])
            deli = [r for r in applied_i if r["公司名称"] == "长风物流"][0]
            check("飞书导入：待一面 → 面试", deli["状态"] == "面试")
            check("飞书导入：轮次的 ISO 日期缩成日期", "一面 2026-09-20" in deli["备注"], deli["备注"])
            proj = [r for r in applied_i if r["公司名称"] == "某公司"][0]
            check("飞书导入：链接列写的不是网址就不当链接（否则渲染成死链）", proj["网页链接"] == "")
            blank = [r for r in applied_i if r["公司名称"] == "未填公司"]
            check("飞书导入：公司名空着用占位名，不瞎猜",
                  len(blank) == 1 and "原表没填公司名" in blank[0]["备注"])
            check("飞书导入：占位那条的备注里留下链接标题（好认是哪个岗位）",
                  "万象商贸（北京）有限公司" in blank[0]["备注"])

            n1, skip1, npend, _plist = import_apps(cfg, rows=parsed)
            check("飞书导入：第一次导入写进 4 条已投的", n1 == 4 and skip1 == 0, "n=%d" % n1)
            check("飞书导入：待投清单单独落盘", npend == 1 and os.path.exists(cfg.pending_csv))
            got_i = load_apps(cfg)
            check("飞书导入：写盘后能读回，状态都合法",
                  len(got_i) == 4 and all(r["状态"] in STATUSES for r in got_i))
            check("飞书导入：来源标记成飞书表", all(r["来源"] == "飞书表" for r in got_i))

            n2, skip2, _, _ = import_apps(cfg, rows=parsed)
            check("飞书导入：再跑一次不会重复导入（幂等）",
                  n2 == 0 and skip2 == 4 and len(load_apps(cfg)) == 4)

            before_i = len(load_apps(cfg))
            import_apps(cfg, dry_run=True, rows=parse_feishu_apps({"ok": True, "data": {
                "fields": icols, "data": [
                    ["新公司", "新岗位", ["已投递"], ["2026-09-29T00:00:00.000+08:00"],
                     None, None, None, None, None, None]]}}))
            check("飞书导入：--dry-run 不落盘", len(load_apps(cfg)) == before_i)

            check("飞书导入：已测评 → 测评笔试", app_map_status("已测评") == "测评笔试")
            check("飞书导入：已挂 → 已结束", app_map_status("已挂") == "已结束")
            check("飞书导入：简历通过 → 简历通过", app_map_status("简历通过") == "简历通过")
            check("飞书导入：没见过的写法有兜底（等消息 → 已投递）", app_map_status("等消息") == "已投递")
            check("飞书导入：认不出的日期不瞎写（返回空）", app_pick_time("改天再说") == "")
        finally:
            cfg.app_csv, cfg.pending_csv = keep_app_csv, keep_pending

        # ---- 投递进度跟飞书表持续同步（表里改状态，看板跟着变） ----
        keep_app_csv, keep_pending = cfg.app_csv, cfg.pending_csv
        keep_sync_json, keep_ttl = cfg.apps_sync_json, cfg.apps_ttl
        keep_app_state = dict(APP_STATE)
        cfg.app_csv = os.path.join(tmp, "sync_apps.csv")
        cfg.pending_csv = os.path.join(tmp, "sync_pending.csv")
        cfg.apps_sync_json = os.path.join(tmp, "apps_sync.json")
        cfg.apps_ttl = 1800
        try:
            scols = ["公司名称", "岗位名称", "最新状态", "投递时间", "投递链接", "备注",
                     "内推码", "地点", "笔试", "一面", "二面", "HR面", "AI面"]

            def feishu_rows(rows):
                return parse_feishu_apps({"ok": True, "data": {"fields": scols, "data": rows}})

            M = ["星桥科技", "电商运营", ["待一面"], ["2026-09-04T00:00:00.000+08:00"],
                 "[职位](https://careers.xingqiao-tech.com/x)", None, ["R7K205"], ["江苏"], None,
                 ["2026-09-20T00:00:00.000+08:00"], None, None, None]
            D = ["长风物流", "管培生", ["OC!"], ["2026-09-10T00:00:00.000+08:00"],
                 "[职位](https://job.changfeng-logistics.com/1)", None, None, None, None, None, None, None, None]
            P = ["云溪银行", "管理培训生", ["未投递"], None, "[收藏](https://x.com/2)",
                 None, None, ["杭州"], None, None, None, None, None]

            # 本地先有一条扩展记的、飞书表里没有的记录
            add_app(cfg, "华宇新能源", "教研运营", "https://job.huayu-energy.com/y", source="扩展")

            ok1, msg1 = sync_apps(cfg, rows=feishu_rows([M, D, P]), quiet=True)
            got = {r["公司名称"]: r for r in load_apps(cfg)}
            check("同步：飞书表里的新记录会补进看板",
                  ok1 and set(got) == {"华宇新能源", "星桥科技", "长风物流"}, str(sorted(got)))
            check("同步：状态跟着飞书走（待一面 → 面试）",
                  got["星桥科技"]["状态"] == "面试", got["星桥科技"]["状态"])
            check("同步：OC! 认成 Offer", got["长风物流"]["状态"] == "Offer", got["长风物流"]["状态"])
            check("同步：把飞书那一格的原文抄进「飞书状态」列",
                  got["星桥科技"]["飞书状态"] == "待一面" and got["长风物流"]["飞书状态"] == "OC!")
            check("同步：轮次日期与内推码照样拼进备注",
                  "一面 2026-09-20" in got["星桥科技"]["备注"] and "内推码 R7K205" in got["星桥科技"]["备注"])
            check("同步：只在本地有的记录（扩展记的）不会被抹掉",
                  got["华宇新能源"]["来源"] == "扩展" and got["华宇新能源"]["状态"] == "已投递")
            with open(cfg.pending_csv, newline="", encoding="utf-8-sig") as _pf:
                _pend = [r.get("公司名称", "") for r in csv.DictReader(_pf)]
            check("同步：飞书表里「未投递」的行不进投递进度",
                  os.path.exists(cfg.pending_csv) and _pend == ["云溪银行"], str(_pend))
            check("同步：落盘后「飞书状态」列读得回来",
                  load_apps(cfg)[0]["飞书状态"] in ("", "待一面", "OC!"))

            # 飞书没动 → 你在看板上手改的状态要保住
            update_app(cfg, got["星桥科技"]["id"], 状态="Offer")
            sync_apps(cfg, rows=feishu_rows([M, D, P]), quiet=True)
            got = {r["公司名称"]: r for r in load_apps(cfg)}
            check("同步：飞书那边没动，看板上手改的状态不会被按回去",
                  got["星桥科技"]["状态"] == "Offer", got["星桥科技"]["状态"])

            # 飞书动了 → 以飞书为准
            M2 = list(M)
            M2[2] = ["待二面"]
            ok2, _ = sync_apps(cfg, rows=feishu_rows([M2, D, P]), quiet=True)
            got = {r["公司名称"]: r for r in load_apps(cfg)}
            check("同步：飞书那边改了状态就以飞书为准（本地那个 Offer 让位）",
                  got["星桥科技"]["状态"] == "面试" and got["星桥科技"]["飞书状态"] == "待二面",
                  got["星桥科技"]["状态"] + "/" + got["星桥科技"]["飞书状态"])
            check("同步：只有真的变了才算「更新」", ok2 and len(_APP_LAST_CHANGES) >= 1,
                  str(len(_APP_LAST_CHANGES)))

            # 链接一样、岗位名改过 → 还是同一条，不重复新增
            D2 = list(D)
            D2[1] = "管培生（市场）"
            n_before = len(load_apps(cfg))
            sync_apps(cfg, rows=feishu_rows([M2, D2, P]), quiet=True)
            check("同步：链接一样、岗位名改过 → 认成同一条，不重复新增",
                  len(load_apps(cfg)) == n_before)

            # 幂等：同一批再来一次不该有变化
            sync_apps(cfg, rows=feishu_rows([M2, D2, P]), quiet=True)
            check("同步：同一批再跑一次没有变化（幂等）", _APP_LAST_CHANGES == [],
                  str(_APP_LAST_CHANGES))

            # 状态词表补齐
            check("状态映射：OC! → Offer", app_map_status("OC!") == "Offer")
            check("状态映射：已放弃 → 已结束（不再占进行中）", app_map_status("已放弃") == "已结束")
            check("状态映射：待HR面 → 面试", app_map_status("待HR面") == "面试")

            # 本机那个透明代理会把 *.feishu.cn 的 TLS 握手掐掉（症状 = 偶发 EOF）。
            # 2026-09-29 实测：裸重试 31 次全败，env -u HTTP_PROXY 一次就通 —— 所以调
            # lark-cli 前必须摘掉代理变量，这条断言防止以后有人把它加回来。
            _e = _lark_env("/x/lark-cli")
            check("调 lark-cli 时摘掉代理变量（否则 feishu.cn 会被本地代理掐 TLS）",
                  not any(k in _e for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy",
                                            "https_proxy", "ALL_PROXY", "all_proxy"))
                  and _e["PATH"].startswith("/x"))

            # lark-cli 是 `#!/usr/bin/env node` 脚本：PATH 里没有 node 就直接
            # `env: node: No such file or directory`。launchd 起的服务 PATH 很干净，
            # 只靠 plist 里写死的那一行 —— 2026-10-02 实测踩过一次：那行指向的 node
            # 版本目录随升级消失，服务活着、页面正常，只有飞书同步静默失败（连
            # 定时同步跑了半天一条没进来）。所以 _lark_env 自己兜一层，这条把它钉住。
            _nd = _node_dir()
            check("调 lark-cli 的 PATH 里一定有 node（launchd 的 PATH 很干净，漏了就静默失败；"
                  "本机没装 node 时这条自然跳过）",
                  (not _nd) or any(os.path.isfile(os.path.join(d, "node"))
                                   for d in _e["PATH"].split(os.pathsep) if d))

            # 旧 CSV（没有「飞书状态」这一列）还得能读
            oldp = os.path.join(tmp, "old_apps.csv")
            with open(oldp, "w", newline="", encoding="utf-8-sig") as f:
                f.write("id,公司名称,岗位名称,状态,投递时间,网页链接,来源,备注,更新时间\n")
                f.write("aaa1,老公司,老岗位,已投递,2026-09-01 10:00,,飞书表,,2026-09-01 10:00\n")
            with open(oldp, newline="", encoding="utf-8-sig") as f:
                _old = list(csv.DictReader(f))
            check("旧 CSV（没有「飞书状态」列）照样读得进来",
                  len(_old) == 1 and (_old[0].get("飞书状态") or "") == "")

            # TTL：过期才拉，没过期不白拉
            APP_STATE.update({"at": time.time(), "ok": True, "msg": "刚刚"})
            check("同步：TTL 内不重复拉飞书", apps_ok_recently(cfg))
            APP_STATE.update({"at": time.time() - cfg.apps_ttl - 1})
            check("同步：超过 TTL 就该去拉", not apps_ok_recently(cfg))

            # 拉不到（没配 base/table）→ 失败不按满 TTL 记，10 分钟后允许重试
            APP_STATE.update({"at": 0.0, "ok": None})
            keep_src = cfg.apps_source
            cfg.apps_source = "feishu"
            okf, _m = sync_apps(cfg, force=True, quiet=True)
            left = cfg.apps_ttl - (time.time() - float(APP_STATE.get("at") or 0))
            check("同步失败不按满 TTL 记（10 分钟后会自己重试）",
                  (not okf) and left <= APP_RETRY_AFTER + 5, "剩余 %.0f 秒" % left)
            cfg.apps_source = keep_src

            # 落盘：重启后不必白拉一次
            APP_STATE.update({"at": time.time(), "ok": True, "msg": "已同步 3 条"})
            write_apps_state(cfg, {"at": APP_STATE["at"], "msg": "已同步 3 条", "count": 3})
            APP_STATE.update({"at": 0.0, "msg": "还没同步过"})
            apps_seed_state(cfg)
            check("同步时间落盘了，重启不会白拉一次",
                  apps_ok_recently(cfg) and "3 条" in str(APP_STATE.get("msg")))
        finally:
            cfg.app_csv, cfg.pending_csv = keep_app_csv, keep_pending
            cfg.apps_sync_json, cfg.apps_ttl = keep_sync_json, keep_ttl
            APP_STATE.clear()
            APP_STATE.update(keep_app_state)

        # ---- 链接反查公司：扩展抓不准时，用看板里的老记录兜 ----
        check("链接归一：去掉查询串与 fragment",
              url_signature("https://app.mokahr.com/campus-recruitment/yunxi/170435#/job/abc")[0]
              == "app.mokahr.com")
        check("链接归一：数字段之后不再往下取",
              url_signature("https://app.mokahr.com/campus-recruitment/yunxi/170435#/job/abc")[1]
              == ["campus-recruitment", "yunxi"])
        check("链接归一：www 去掉、大小写拉平",
              url_signature("https://WWW.Example.com/A/B.html") == ("example.com", ["a", "b"]))
        check("链接归一：不是网址就返回空",
              url_signature("项目管理") == ("", []) and url_signature("") == ("", []))

        same = "https://app.mokahr.com/campus-recruitment/yunxi/170435"
        check("同一公司下的两个职位链接算吻合",
              url_matches(same + "#/job/90611de8-f66d", same + "#/job/other-id") == 2)
        check("换个子域名就不算（zhiye 每家一个域名）",
              url_matches("https://yunxi.zhiye.com/campus/jobs",
                          "https://changfeng.zhiye.com/campus/jobs") == 0)
        check("同一平台上不同公司的页面不算（51job）",
              url_matches("https://campus.51job.com/xingqiao2027/job.html",
                          "https://campus.51job.com/changfeng2027MT/p3.html") == 0)
        check("只有通用段吻合也不算（/campus/jobs 每家都一样）",
              url_matches("https://hr.example.com/campus/jobs",
                          "https://hr.example.com/campus/other") == 0)
        check("不同网站的链接不算", url_matches("https://a.com/x/y", "https://b.com/x/y") == 0)
        check("完全一样算最吻合", url_matches(same, same) == 2)

        # 托管平台：同一家公司的岗位页与成功页路径完全不同，靠链接里的公司标识参数认
        CT = "d7527e79-29e3-42ba-b0ce-dd5d3dc173e4"
        yjs_job = f"https://young.yingjiesheng.com/xyzjob?ctmid={CT}&jobid=28071"
        yjs_ok = f"https://young.yingjiesheng.com/xyzsuccess?ctmid={CT}&resumeid=e2&aid=28071"
        yjs_other = "https://young.yingjiesheng.com/xyzsuccess?ctmid=aaaa1111bbbb&aid=1"
        check("公司标识参数：取得出来（连字符去掉）",
              url_tenant(yjs_ok) == "d7527e7929e342bab0cedd5d3dc173e4")
        check("公司标识参数：简历 id / 投递 id 不算（那是「谁投的」）",
              url_tenant("https://x.com/a?resumeid=e277576b-4c6a&aid=28071") == "")
        check("同一公司：岗位页与成功页路径不同也认（应届生系统）",
              url_matches(yjs_job, yjs_ok) >= 2)
        check("不同公司：标识参数不一样就不认",
              url_matches(yjs_ok, yjs_other) == 0)

        keep_guess_csv = cfg.app_csv
        cfg.app_csv = os.path.join(tmp, "guess_apps.csv")
        try:
            _write_all(cfg, [
                {"id": "g1", "公司名称": "Yunxi", "岗位名称": "推荐投", "状态": "已投递",
                 "投递时间": "2026-09-03 00:00",
                 "网页链接": "https://app.mokahr.com/campus-recruitment/yunxi/170435#/job/90611de8-f66d",
                 "来源": "飞书表", "备注": "", "更新时间": ""},
                {"id": "g2", "公司名称": "万象商贸（北京）有限公司", "岗位名称": "销售经理",
                 "状态": "已投递", "投递时间": "2026-09-26 20:40",
                 "网页链接": "https://app.mokahr.com/campus-recruitment/wanxiang/143541#/candidateHome/applications",
                 "来源": "飞书表", "备注": "", "更新时间": ""},
                {"id": "g3", "公司名称": "星桥科技", "岗位名称": "投资管理岗", "状态": "已投递",
                 "投递时间": "2026-09-29 17:30",
                 "网页链接": ("https://young.yingjiesheng.com/xyzjob?ctmid=d7527e79-29e3-42ba"
                              "&jobid=28071"),
                 "来源": "扩展", "备注": "", "更新时间": ""},
                {"id": "g4", "公司名称": "某某科技有限公司", "岗位名称": "内容运营", "状态": "已投递",
                 "投递时间": "2026-09-20 10:00",
                 "网页链接": "https://jobs.solo-corp.com/apply/1",
                 "来源": "飞书表", "备注": "", "更新时间": ""},
            ])
            g = guess_from_url(cfg, "https://app.mokahr.com/campus-recruitment/yunxi/170435#/job/xxx")
            check("反查：mokahr 同公司另一个职位 → 认出 Yunxi",
                  g["company"] == "Yunxi" and g["matched"] == "prefix" and g["confident"], str(g))
            check("反查：职位 id 不同，不硬抄老岗位名", g["position"] == "", str(g))
            g2 = guess_from_url(cfg, "https://app.mokahr.com/campus-recruitment/wanxiang/143541#/job/yyy")
            check("反查：同一平台另一家公司不会串（wanxiang → 万象商贸）",
                  g2["company"] == "万象商贸（北京）有限公司", str(g2))
            g3 = guess_from_url(cfg, "https://app.mokahr.com/campus-recruitment/newco/999#/job/zzz")
            check("反查：没见过的公司链接 → 不下结论（宁空不错）",
                  g3["company"] == "" and g3["matched"] == "none", str(g3))
            gex = guess_from_url(cfg, "https://app.mokahr.com/campus-recruitment/yunxi/170435"
                                      "#/job/90611de8-f66d")
            check("反查：同一条链接 → exact，连岗位名一起给",
                  gex["matched"] == "exact" and gex["company"] == "Yunxi"
                  and gex["position"] == "推荐投", str(gex))
            check("反查：不是网址直接返回空",
                  guess_from_url(cfg, "项目管理")["matched"] == "none")
            gy = guess_from_url(cfg, ("https://young.yingjiesheng.com/xyzsuccess"
                                      "?ctmid=d7527e79-29e3-42ba&resumeid=e2&aid=28071"))
            check("反查：托管平台的成功页 → 认出同 ctmid 的公司（星桥科技）",
                  gy["company"] == "星桥科技" and gy["confident"]
                  and gy["matched"] == "prefix", str(gy))
            check("反查：路径没共同点就不硬抄老岗位名", gy["position"] == "", str(gy))
            gh = guess_from_url(cfg, "https://jobs.solo-corp.com/other/page")
            check("反查：整站只投过一家 → 给参考值但不标「确信」",
                  gh["company"] == "某某科技有限公司" and gh["matched"] == "host"
                  and not gh["confident"], str(gh))
            check("反查：空记录也不炸",
                  guess_from_url(Config(None, base=tmp), "https://x.com/a/b")["company"] == "")
        finally:
            cfg.app_csv = keep_guess_csv

        # 拿她自己的公司名单当字典在页面文字里认公司（扩展弹卡前预填用）。
        # 合成名字，别拿她表里的真公司来测。
        _names = ["星桥科技", "云溪智能", "云溪", "长风物流股份有限公司", "万象商贸"]
        _r1 = pick_company_hit("恭喜你已成功投递该企业！星桥科技 2027 校园招聘", _names)
        check("认公司：长名字优先（别把「云溪」当成「云溪智能」）",
              pick_company_hit("云溪智能 2027 校招", _names)["hit"] == "云溪智能",
              str(pick_company_hit("云溪智能 2027 校招", _names)))
        check("认公司：页面上没提任何一家 → 不硬编一个",
              pick_company_hit("这是一段跟公司无关的文字", _names)["hit"] == "")
        # 两个字的名字太容易是巧合（页脚的合作方、导航里的别家），
        # 除非整页只命中这一个，否则不替她填、只放进备选让她点
        _r2 = pick_company_hit("合作方：云溪 星桥科技", _names)
        check("认公司：短名字和长名字同时命中时，短的只进备选不自动填",
              _r2["hit"] == "星桥科技" and "云溪" in _r2["cands"], str(_r2))
        _r3 = pick_company_hit("云溪 2027 校园招聘", _names)
        check("认公司：整页只命中一个短名字 → 也认（不然二字公司全废）",
              _r3["hit"] == "云溪" and _r3["sure"], str(_r3))
        check("认公司：空文字 / 空名单都不炸",
              pick_company_hit("", _names)["hit"] == "" and
              pick_company_hit("星桥科技", [])["hit"] == "" and
              pick_company_hit("星桥科技", ["", "  "])["hit"] == "")
        _r4 = pick_company_hit("长风物流股份有限公司 招聘", ["长风物流", "长风物流股份有限公司"])
        check("认公司：互为子串时取更具体的那个",
              _r4["hit"] == "长风物流股份有限公司", str(_r4))
        check("认公司：页面文字里的空格不影响认名（「星桥 科技」也算）",
              pick_company_hit("星桥 科技 2027 校招", _names)["hit"] == "星桥科技",
              str(pick_company_hit("星桥 科技 2027 校招", _names)))

        # 处理状态的本地覆盖层：看板上手改的状态不能被飞书同步冲掉
        k1 = notice_state_key(notices[0])
        check("通知 key 稳定（同样内容两次一样）", k1 and k1 == notice_state_key(notices[0]))
        check("通知 key 区分不同条目", k1 != notice_state_key(notices[1]))
        check("key 不受类型是 list 还是字符串影响",
              notice_state_key({"公司名称": "云溪智能（杭州）网络有限公司", "通知类型": "测评、笔试",
                                "通知截止时间": "2026-09-30 23:59", "邮件主题": "测评邀请"}) == k1)

        check("空状态列表 → 没有覆盖", apply_notice_states(cfg, notices) == 0)
        check("非法状态被拒", not set_notice_state(cfg, k1, "瞎写的状态"))
        check("空 key 被拒", not set_notice_state(cfg, "", "已完成"))
        check("写入覆盖成功", set_notice_state(cfg, k1, "已完成"))
        check("覆盖能读回", load_notice_states(cfg).get(k1) == "已完成")

        fresh = load_notices(cfg)
        n_over = apply_notice_states(cfg, fresh)
        first = [n for n in fresh if n["_key"] == k1][0]
        check("覆盖盖到通知上", n_over == 1 and first["处理状态"] == "已完成")
        check("覆盖时标出状态来源是本地", first["状态来源"] == "本地")
        check("原始处理状态留底", first["原始处理状态"] == "")

        check("传空 = 撤掉覆盖", set_notice_state(cfg, k1, "") and k1 not in load_notice_states(cfg))
        check("撤掉后回到原值", apply_notice_states(cfg, load_notices(cfg)) == 0)

        # 看板页在飞书模式下每次同步都重写 CSV，覆盖必须活下来
        set_notice_state(cfg, k1, "已忽略")
        write_notices_csv(cfg, [
            {"公司名称": "云溪智能（杭州）网络有限公司", "岗位名称": "内容运营", "通知类型": ["测评", "笔试"],
             "通知截止时间": "2026-09-30 23:59", "邮件链接": "https://x/1", "邮件主题": "测评邀请",
             "邮件收到时间": "2026-09-26 09:00", "处理状态": ""},
        ])
        survived = load_notices(cfg)
        apply_notice_states(cfg, survived)
        check("飞书重写 CSV 后手动状态还在", survived[0]["处理状态"] == "已忽略")

        payload = board_payload(cfg)
        check("看板 meta 带处理状态选项", payload["meta"]["notice_states"] == NOTICE_STATES)
        check("看板 meta 报出覆盖条数", payload["meta"]["notice_overrides"] == 1)
        check("已了结的状态集合给到前端", set(payload["meta"]["notice_closed"]) == set(NOTICE_CLOSED))
        check("看板 meta 报出环节标准次序（进度视图那排筛选按钮要按它排，不能按中文拼音序；"
              "给 INTERVIEW_ORDER 而不是 LADDER —— 笔试/群面/三面也得排得进去）",
              payload["meta"]["iv_order"] == INTERVIEW_ORDER and
              set(INTERVIEW_LADDER) <= set(INTERVIEW_ORDER))

        # ---- v0.35.0：手动补的通知（只存本机 data/notices_manual.json，不回写飞书）----
        # 用一个**独立临时目录**，别把手动行漏进上面那些按条数断言的用例里。
        _mtmp = tempfile.mkdtemp(prefix="at-manual-")
        mcfg = Config(None, base=_mtmp)
        mcfg.notices_csv = os.path.join(_mtmp, "notices.csv")
        _mpath = notices_manual_path(mcfg)

        check("手动通知：文件不存在时 load 返回 []（不让看板因此打不开）",
              not os.path.exists(_mpath) and load_manual_notices(mcfg) == [])

        _ok, _err, mid1 = add_manual_notice(
            mcfg, 公司名称="示例公司", 岗位名称="后端开发", 类型="面试",
            截止时间="2026-10-16 12:00", 链接="hr.example.com/t/abc",
            说明="短信：请于10月16日12:00前完成面试")
        _mrows = load_manual_notices(mcfg)
        check("手动通知：读 / 写往返，字段落对（类型是 list、说明进「邮件主题」、创建时间有、"
              "链接没带协议自动补 https://）",
              _ok and len(mid1) == 8 and len(_mrows) == 1 and
              _mrows[0]["公司名称"] == "示例公司" and _mrows[0]["通知类型"] == ["面试"] and
              _mrows[0]["通知截止时间"] == "2026-10-16 12:00" and
              _mrows[0]["邮件链接"] == "https://hr.example.com/t/abc" and
              _mrows[0]["邮件主题"] == "短信：请于10月16日12:00前完成面试" and
              bool(_mrows[0].get("创建时间")), str(_mrows))

        with open(_mpath, "w", encoding="utf-8") as _f:
            _f.write("{坏掉的 json")
        check("手动通知：文件内容坏掉时 load_manual_notices / load_notices 都返回 []（不抛异常）",
              load_manual_notices(mcfg) == [] and load_notices(mcfg) == [])
        save_manual_notices(mcfg, _mrows)          # 把被写坏的文件修回来再继续

        check("手动通知：公司名空 → 失败",
              add_manual_notice(mcfg, 公司名称="  ", 类型="面试")[0] is False)
        check("手动通知：类型不在名单 → 失败",
              add_manual_notice(mcfg, 公司名称="示例公司2", 类型="瞎写的")[0] is False)
        check("手动通知：截止时间格式不对 → 失败（只收空格分隔，T / 斜杠都不认）",
              add_manual_notice(mcfg, 公司名称="示例公司3", 截止时间="2026/10/16")[0] is False and
              add_manual_notice(mcfg, 公司名称="示例公司3", 截止时间="2026-10-16T12:00")[0] is False)

        _okd, _, midd = add_manual_notice(
            mcfg, 公司名称="示例公司", 岗位名称="后端开发", 类型="面试",
            截止时间="2026-10-16 12:00", 链接="hr.example.com/t/abc",
            说明="短信：请于10月16日12:00前完成面试")
        check("手动通知：完全重复的行复用旧 id、不新增（只跟手动表内部比）",
              _okd and midd == mid1 and len(load_manual_notices(mcfg)) == 1)

        # 手动行并进 load_notices：带齐 来源=手动 / _mid / _key / _gkey；飞书（CSV）行一个字段都没变
        with open(mcfg.notices_csv, "w", newline="", encoding="utf-8-sig") as _f:
            _w = csv.writer(_f)
            _w.writerow(["公司名称", "岗位名称", "通知类型", "通知截止时间", "邮件链接",
                         "邮件主题", "邮件收到时间", "处理状态"])
            _w.writerow(["星桥科技", "数据", "测评", "2026-10-20 18:00", "https://x/m1",
                         "测评邀请", "2026-10-10 09:00", ""])
        _merged = load_notices(mcfg)
        _man = [r for r in _merged if r.get("来源") == "手动"]
        _csv = [r for r in _merged if r.get("来源") != "手动"]
        check("手动通知：并进 load_notices 后带齐 来源=手动 / _mid / _key / _gkey",
              len(_merged) == 2 and len(_man) == 1 and _man[0]["_mid"] == mid1 and
              _man[0]["_key"] == notice_state_key(_man[0]) and _man[0]["_gkey"] == "示例公司",
              str(_merged))
        check("手动通知：飞书来的行一个字段都没变（还是老字段，没被手动行带跑）",
              len(_csv) == 1 and _csv[0]["公司名称"] == "星桥科技" and
              _csv[0]["通知类型"] == ["测评"] and _csv[0]["_gkey"] == "星桥科技" and
              "来源" not in _csv[0] and "_mid" not in _csv[0])

        check("手动通知：set_notice_type 能改到手动行的类型（原来只认 notices.csv，会漏掉手动行）",
              set_notice_type(mcfg, _man[0]["_key"], "测评") and
              load_manual_notices(mcfg)[0]["通知类型"] == ["测评"])

        _cur_key = notice_state_key(load_manual_notices(mcfg)[0])
        set_notice_state(mcfg, _cur_key, "已完成")
        check("手动通知：删掉后从表里消失",
              del_manual_notice(mcfg, mid1) and load_manual_notices(mcfg) == [])
        check("手动通知：删掉时顺手清掉挂在这条上的处理状态（不留孤儿）",
              _cur_key not in load_notice_states(mcfg))
        check("手动通知：删不存在的 id 返回 False",
              del_manual_notice(mcfg, "nope-not-exist") is False)

        _mh = render_html(mcfg)
        check("看板页有手动添加的前端接线（按钮 / 面板 / 两个接口 / 删除入口 / 手动标签）",
              'id="ntAdd"' in _mh and 'id="ntAddBox"' in _mh and
              "/api/notice-add" in _mh and "/api/notice-del" in _mh and
              'class="ntdel"' in _mh and '=== "手动"' in _mh)
        shutil.rmtree(_mtmp, ignore_errors=True)

        html = render_html(cfg)
        for probe, what in [("ntSortUrg", "排序按钮"), ("ntvDl", "视图切换（面试测评截止）"),
                            ("ntvIv", "视图切换（投递进度）"), ("ntSortDl", "按截止时间排序"),
                            ("class=\"sts", "处理状态下拉"),
                            ("只看没处理的", "只看没处理的开关"), ("只看紧急的", "只看紧急的开关"),
                            ("id=\"ntAdd\"", "手动添加按钮"), ("id=\"ntAddBox\"", "手动添加面板")]:
            check("看板页有" + what, probe in html)
        set_notice_state(cfg, k1, "")

        # 飞书矩阵 → 通知列表
        fake = {
            "ok": True,
            "data": {
                "fields": ["公司名称", "备注", "邮件链接", "邮件收到时间", "通知类型",
                           "邮件主题", "岗位名称", "通知截止时间", "处理状态", "距截止天数"],
                "data": [
                    ["星桥科技", None,
                     "[https://bsurl.cn/v2/GeUH9Xy9](https://bsurl.cn/v2/GeUH9Xy9)",
                     "2026-09-18T13:14:00.000+08:00", ["测评"],
                     "请参加星桥科技2027UFLP管理培训生项目在线测评", "市场部",
                     "2026-09-21T13:14:00.000+08:00", ["已完成"], ""],
                    ["", "公司名为空的行该被丢掉", "", "", [], "", "", "", [], ""],
                ],
            },
        }
        fe = parse_feishu_payload(fake)
        check("飞书矩阵解析：丢掉没有公司名的行", len(fe) == 1)
        check("飞书 ISO 时间 → YYYY-MM-DD HH:MM",
              fe and fe[0]["通知截止时间"] == "2026-09-21 13:14",
              fe[0]["通知截止时间"] if fe else "空")
        check("飞书单选数组 → 类型列表", bool(fe) and fe[0]["通知类型"] == ["测评"])
        check("飞书 markdown 链接还原成纯 URL",
              bool(fe) and fe[0]["邮件链接"] == "https://bsurl.cn/v2/GeUH9Xy9",
              fe[0]["邮件链接"] if fe else "空")
        check("飞书处理状态读出来", bool(fe) and fe[0]["处理状态"] == "已完成")

        # 飞书同步写出的缓存 CSV，要能被 load_notices 原样读回
        with tempfile.TemporaryDirectory() as tmp2:
            cfg2 = Config(None, base=tmp2)
            cfg2.notices_csv = os.path.join(tmp2, "notices.csv")
            write_notices_csv(cfg2, fe)
            back = load_notices(cfg2)
            check("飞书写出的 CSV 能被读回",
                  len(back) == 1 and back[0]["通知类型"] == ["测评"]
                  and back[0]["处理状态"] == "已完成", str(back))

        # source = auto 的判断
        with tempfile.TemporaryDirectory() as tmp3:
            p3 = os.path.join(tmp3, "feishu.ini")
            with open(p3, "w", encoding="utf-8") as f:
                f.write("[notices]\nfeishu_base_token = B\nfeishu_table_id = T\n")
            c3 = Config(p3, base=tmp3)
            check("配了飞书表 → source 自动切成 feishu",
                  c3.notices_source == "feishu", c3.notices_source)
            check("飞书模式的缓存落在 data/notices.csv",
                  c3.notices_csv.endswith(os.path.join("data", "notices.csv")), c3.notices_csv)
            check("不填 profile 时不带 --profile", c3.feishu_profile == "")

            p4 = os.path.join(tmp3, "file.ini")
            with open(p4, "w", encoding="utf-8") as f:
                f.write("[notices]\nsource = file\ncsv = x.csv\n")
            c4 = Config(p4, base=tmp3)
            check("source = file 时不去碰飞书",
                  c4.notices_source == "file" and c4.notices_csv.endswith("x.csv"),
                  c4.notices_csv)

        payload = board_payload(cfg)
        html = render_html(cfg)
        check("看板页含占位符已替换",
              "__DATA__" not in html and "__OPPS__" not in html and "__APPVER__" not in html and
              "投递进度" in html and "邮箱通知" in html)
        check("看板页数据可解析", payload["apps"][0]["公司名称"] and "关联" in payload["apps"][0])
        check("看板页里没有模板残留", "__META__" not in html)

        # ---- 页面自己发现「我这份代码过期了」----
        # 她从来不去硬刷新（页脚版本只在加载时渲染一次），所以「版本对不上」必须由**页面自己**发现：
        # /api/board 每次轮询都带回服务端当前版本，拿它跟页面里印的这一版比就知道了。
        check("页面里印着「我这一版是哪个版本」（占位符已换成真版本号）",
              'let PAGE_VERSION = "%s"' % APP_VERSION in html)
        check("每次轮询都会核一遍版本（pull 里调 verTick）",
              "renderApps(); renderNotices();" in html and "verTick();" in html)
        check("版本对不上就亮出刷新提示条（平时 hidden、固定右下角、写明新版本号）",
              'id="verNew"' in html and "#verNew{position:fixed" in html and
              "看板已更新到 <b>v" in html)
        check("提示条只在**没在敲东西**时才自动刷新（改着格子 / 光标在输入框里绝不刷，别吞她的输入）",
              'document.querySelector("td.editing")' in html and
              't === "INPUT" || t === "TEXTAREA" || t === "SELECT"' in html and
              "!document.hidden" in html)
        check("自动刷新只做一次（不会刷成循环）+ 刷新前记住滚动位置、回来放回去",
              "let verReloaded = false;" in html and
              'sessionStorage.setItem("at.reload.y"' in html and
              'sessionStorage.getItem("at.reload.y")' in html)
        check("自动刷新走单独的 verDoReload（board-check 才能打桩，不真刷新地验证它）",
              "function verDoReload(){" in html and "setTimeout(verDoReload, 900)" in html)

        # ---- 机会库 ----
        check("没配机会库表 → source 是 off", Config(None, base=tmp).opps_source == "off")
        p5 = os.path.join(tmp, "opp.ini")
        with open(p5, "w", encoding="utf-8") as f:
            f.write("[opportunities]\nfeishu_base_token = BT\nfeishu_table_id = TT\nfeishu_ttl = 600\n")
        c5 = Config(p5, base=tmp)
        check("配了机会库表 → source 切成 feishu", c5.opps_source == "feishu", c5.opps_source)
        check("机会库 TTL 读得到", c5.opp_ttl == 600, str(c5.opp_ttl))
        check("机会库缓存落在 data/opportunities.json",
              c5.opps_json.endswith("opportunities.json"), c5.opps_json)

        # ---- 2026-10-08 AC-09：首启把随包快照预置进 data/（三条都真跑，不真跑就等于没写）----
        # 夹具快照只有 3 条 —— 刻意不用仓库里那份真的（6.5MB / 9381 条）：
        # 一是快，二是「条数对」这个断言必须对着一个**已知的数**才叫断言。
        # ⚠️ 三条都要用**自己专用的 base**，别用上面共用的 tmp —— 这条 hook 钩在
        # load_opportunities 上，自检前面对别的临时目录调过它时，真快照（仓库里真有）已经
        # 被预置进那些目录了；用专用 base 才是「data/ 真的为空」这个前提。
        _ac9 = os.path.join(tmp, "ac09")
        _snap_dir = os.path.join(_ac9, "snapshot")
        _snap_src = os.path.join(_snap_dir, "opportunities.json")
        os.makedirs(_snap_dir, exist_ok=True)
        with open(_snap_src, "w", encoding="utf-8") as _f:
            json.dump({"synced_at": "2026-10-08 09:00", "count": 3,
                       "rows": [{"company": "甲公司"}, {"company": "乙公司"}, {"company": "丙公司"}]},
                      _f, ensure_ascii=False)
        global _opps_snapshot_path
        _keep_snap = _opps_snapshot_path
        try:
            _opps_snapshot_path = (lambda p=_snap_src: p)   # 指到夹具，别把真快照拷进临时目录
            _c9 = Config(None, base=_ac9)
            _n = seed_opportunities_snapshot(_c9)
            _rows, _ = load_opportunities(_c9)
            check("首启预置：data/ 没有、包里带了快照 → 拷成，条数从快照里数出来（不是写死的）",
                  _n == 3 and len(_rows) == 3 and os.path.exists(_c9.opps_json), "拷进 %d 条" % _n)
            _mtime = os.path.getmtime(_c9.opps_json)
            _before = open(_c9.opps_json, encoding="utf-8").read()
            check("首启预置：data/ 已经有了 → **原样不动**（用户自己同步的才是真相，绝不覆盖）",
                  seed_opportunities_snapshot(_c9) == 0 and
                  os.path.getmtime(_c9.opps_json) == _mtime and
                  open(_c9.opps_json, encoding="utf-8").read() == _before)
            # 第三条：快照不在（monkeypatch 指到一个不存在的路径），base 也换新的
            _opps_snapshot_path = (lambda: os.path.join(_ac9, "snapshot", "不存在.json"))
            _c9b = Config(None, base=os.path.join(_ac9, "nosnap"))
            os.makedirs(_c9b.base, exist_ok=True)
            check("首启预置：快照缺失 → 静默跳过（不报错、不在 data/ 里建空文件）",
                  seed_opportunities_snapshot(_c9b) == 0 and not os.path.exists(_c9b.opps_json))
        finally:
            _opps_snapshot_path = _keep_snap

        # 同步失败不能按满 TTL 记，否则 6 小时内再也不重试
        global fetch_opportunities_from_feishu
        _keep_fetch = fetch_opportunities_from_feishu
        _keep_state = dict(OPP_STATE)
        _keep_ttl = c5.opp_ttl
        try:
            def _boom(cfg_):
                raise RuntimeError("lark-cli 掉线")
            fetch_opportunities_from_feishu = _boom
            c5.opp_ttl = 21600                      # 按真实默认值算，重试窗口应该是 10 分钟
            sync_opportunities(c5, force=True, quiet=True)
            _left = c5.opp_ttl - (time.time() - OPP_STATE["at"])
            check("机会库：同步失败后重试窗口远短于整段 TTL（不是等满 6 小时）",
                  OPP_RETRY_AFTER - 60 <= _left <= OPP_RETRY_AFTER + 5,
                  "还剩 %.0f 秒（期望 ≈%d）" % (_left, OPP_RETRY_AFTER))
            check("机会库：失败状态标了 ok=False（页面能提示）", OPP_STATE.get("ok") is False)
            check("机会库：失败原因留在 msg 里", "lark-cli 掉线" in str(OPP_STATE.get("msg")))
        finally:
            fetch_opportunities_from_feishu = _keep_fetch
            c5.opp_ttl = _keep_ttl
            OPP_STATE.clear()
            OPP_STATE.update(_keep_state)

        check("截止时间：具体日期能解析", opp_deadline_ts("2026-10-15") is not None)
        check("截止时间：招满为止没有日期", opp_deadline_ts("招满为止") is None)
        check("截止时间：取文本里最晚的那个日期",
              opp_deadline_ts("2026-03-01 至 2026-04-20") == opp_deadline_ts("2026-04-20"))
        check("截止时间：月日写法也能认", opp_deadline_ts("10月15日") is not None)

        # ---- v0.35.1：当天截止 ≠ 已过期（2026-10-11 实测当天截止的 31 条全被藏起来）----
        # 老口径把「10/15」解析成当天 00:00，「还没截止」的判据全是 dl < now ——
        # 一过零点，当天截止的岗位立刻被当过期藏掉。「截止 10/15」意思是 10/15 一整天都能投，
        # 所以解析结果统一改成**当天末刻**（23:59:59）。
        check("截止时间：具体日期解析成当天末刻（23:59:59），不再是一过零点就算过期",
              opp_deadline_ts("2026-10-15") == datetime(2026, 10, 15, 23, 59, 59).timestamp())
        check("截止时间：末刻严格落在 [当天 00:00, 次日 00:00) 里",
              datetime(2026, 10, 15).timestamp()
              < opp_deadline_ts("2026-10-15") < datetime(2026, 10, 16).timestamp())
        check("截止时间：月日写法也走末刻（取时部分是 23:59:59）",
              (opp_deadline_ts("10月15日") is not None)
              and datetime.fromtimestamp(opp_deadline_ts("10月15日")).strftime("%H:%M:%S") == "23:59:59",
              str(opp_deadline_ts("10月15日")))
        # 行为级：造一条截止日=今天的 row，带 open=1 查「还没截止」，它必须在结果里
        _d_now = datetime.now()
        _dstr_today = "%04d-%02d-%02d" % (_d_now.year, _d_now.month, _d_now.day)
        _ts_mid_today = datetime(_d_now.year, _d_now.month, _d_now.day).timestamp()
        _row_today = parse_opp_record({"record_id": "recToday", "fields": {
            "公司名称": "当天截止测试公司", "招聘岗位": "运营",
            "截止时间": _dstr_today, "更新时间": "2026/10/01"}})
        _row_today["upd"] = parse_opp_date("2026/10/01")
        _q_today = query_opportunities([_row_today], [], {"open": ["1"], "limit": ["200"]})
        check("机会库：「还没截止」查得到截止日=今天的岗位（原 bug：当天截止的全被当过期藏）",
              _q_today["total_rows"] == 1
              and any(r.get("company") == "当天截止测试公司" for r in _q_today["rows"]),
              "total_rows=%s" % _q_today.get("total_rows"))
        # dl 是持久化在 opportunities.json 里的：装载时必须按当前口径重算，
        # 否则要等下一次飞书同步（TTL 6 小时）才生效，她今天就看不到那几十条。
        _opp_tmp_json = os.path.join(tmp, "opps_today.json")
        with open(_opp_tmp_json, "w", encoding="utf-8") as _f:
            json.dump({"rows": [{"id": "recToday", "company": "当天截止测试公司",
                                 "deadline": _dstr_today, "updated": _dstr_today,
                                 "dl": _ts_mid_today}],       # 故意写老口径：当天 00:00
                       "synced_at": ""}, _f, ensure_ascii=False)
        _OPPS_CACHE.update({"path": "", "mtime": 0.0, "rows": []})   # 别撞上别的用例留下的缓存
        _cfg_opp = Config(None, base=tmp)
        _cfg_opp.opps_json = _opp_tmp_json
        _rows_loaded, _ = load_opportunities(_cfg_opp)
        check("机会库：装载时按当前口径重算 dl —— 缓存里那份老口径的「当天 00:00」被改成末刻",
              bool(_rows_loaded) and _rows_loaded[0]["dl"] == opp_deadline_ts(_dstr_today)
              and _rows_loaded[0]["dl"] > _ts_mid_today,
              str(_rows_loaded[0].get("dl") if _rows_loaded else None))
        _OPPS_CACHE.update({"path": "", "mtime": 0.0, "rows": []})   # 收尾，别影响后面的用例

        raw = {"record_id": "recX", "fields": {
            "公司名称": "宏图地产", "行业大类": ["新能源"], "企业性质": ["民企"],
            "地点②-筛地点选这里！！！！！！": "北京,苏州",
            "官方公告": "[https://a/b](https://a/b)", "招聘岗位": "算法实习生、产品运营",
            "截止时间": "2026-12-31", "投递方式": "[https://q/z](https://q/z)",
            "招聘对象": ["2027届"], "批次": ["秋招"], "文本 7": ["含免笔试"],
            "更新时间": "2026/08/08"}}
        r5 = parse_opp_record(raw)
        check("机会库：markdown 链接还原成纯 URL",
              r5["apply"] == "https://q/z" and r5["notice"] == "https://a/b", str(r5["apply"]))
        check("机会库：地点公式列按逗号拆开", r5["cities"] == ["北京", "苏州"], str(r5["cities"]))
        check("机会库：没有公司名的行丢掉",
              parse_opp_record({"fields": {"公司名称": ""}}) is None)

        far = time.time() + 30 * 86400
        d0s = day_start()
        orows = [dict(r5, upd=d0s),                        # 今天更新
                 dict(r5, id="o2", company="某某科技", industries=["金融业"],
                      cities=["广州"], dl=far, targets=["2026届"],
                      upd=d0s - 2 * 86400),                # 2 天前更新
                 dict(r5, id="o3", company="已截止公司", industries=["金融业"],
                      cities=["深圳"], dl=time.time() - 86400,
                      upd=None)]                           # 更新时间认不出来
        oapp = [{"公司名称": "宏图地产", "岗位名称": "算法实习生"}]
        st5 = opp_stats(orows, oapp)
        check("机会库：KPI 口径对得上",
              st5 == {"all": 3, "open": 2, "y2027": 2, "mine": 1, "exam": 3,
                      "new1": 1, "new3": 2, "new7": 2}, str(st5))
        # 「最近新开」：只看最近 N 天内更新过的；认不出更新时间的一律不要
        check("机会库：「最近新开」3 天内筛出 2 条",
              query_opportunities(orows, oapp, {"newdays": ["3"]})["total"] == 2)
        check("机会库：「最近新开」今天档只剩今天那条",
              query_opportunities(orows, oapp, {"newdays": ["1"]})["total"] == 1)
        check("机会库：「最近新开」档位非法时当作不筛",
              query_opportunities(orows, oapp, {"newdays": ["abc"]})["total"] == 3)
        check("机会库：「最近新开」天数超范围会被夹到 365 天",
              query_opportunities(orows, oapp, {"newdays": ["99999"]})["total"] == 2)
        check("机会库：按更新时间排序认得出先后（今天的在 2 天前的前面，认不出的沉底）",
              [r.get("id") for r in query_opportunities(
                  orows, oapp, {"sort": ["up"]})["rows"]] == [orows[0].get("id"), "o2", "o3"],
              str([r.get("id") for r in query_opportunities(orows, oapp, {"sort": ["up"]})["rows"]]))
        # 源表的选项列被塞过脏值（表头提示、公司名），解析时要滤掉
        r6 = parse_opp_record({"fields": {
            "公司名称": "某某", "招聘对象": ["26届（留）-27届", "所属：校招一表通"],
            "批次": ["筛选前请先看必读文档", "秋招"], "文本 7": ["永业行", "含免笔试"]}})
        check("机会库：招聘对象只留届数，跨届全留",
              r6["targets"] == ["2026届", "2027届"], str(r6["targets"]))
        check("机会库：批次/笔试列的脏值被滤掉",
              r6["batch"] == ["秋招"] and r6["exam"] == ["含免笔试"],
              "%s / %s" % (r6["batch"], r6["exam"]))
        check("机会库：只看含免笔试",
              query_opportunities(orows, oapp, {"exam": ["含免笔试"]})["total"] == 3)
        check("机会库：只看未截止",
              query_opportunities(orows, oapp, {"open": ["1"]})["total"] == 2)
        res = query_opportunities(orows, oapp, {"city": ["广州"]})
        check("机会库：按城市筛", res["total"] == 1 and res["rows"][0]["company"] == "某某科技")
        check("机会库：只看我投过的公司",
              query_opportunities(orows, oapp, {"mine": ["1"]})["total"] == 1)
        check("机会库：搜索命中岗位文本",
              query_opportunities(orows, oapp, {"q": ["算法"]})["total"] == 3)
        res = query_opportunities(orows, oapp, {"limit": ["10"], "offset": ["1"]})
        check("机会库：分页切片生效", len(res["rows"]) == 2 and res["offset"] == 1)

        # —— v0.25.0：同一家公司不同分行并成一行（她的原话：「展现的时候把同一家公司
        #    不同分行放在同一个公司下面」）——
        obr = [dict(r5, id="c0", company="星辰银行", dl=far + 3 * 86400),
               dict(r5, id="c1", company="星辰银行上海分行", cities=["上海"], dl=far + 2 * 86400),
               dict(r5, id="c2", company="星辰银行北京分行", cities=["北京"], dl=far),
               dict(r5, id="c3", company="明月期货台州营业部", cities=["台州"], dl=far),
               dict(r5, id="c4", company="明月期货杭州营业部", cities=["杭州"], dl=far),
               dict(r5, id="c5", company="星河银行上海分行", cities=["上海"], dl=far),
               dict(r5, id="c6", company="红星教育", cities=["北京"], dl=far)]
        opp_assign_keys(obr)
        _bg = query_opportunities(obr, [], {"sort": ["co"]})
        check("机会库：同一家公司的各分行并成一家（7 条 → 4 家）",
              _bg["total"] == 4 and _bg["total_rows"] == 7,
              "%d 家 / %d 条" % (_bg["total"], _bg["total_rows"]))
        _st = [g for g in _bg["groups"] if g["company"] == "星辰银行"]
        check("机会库：一家银行 3 条挂在一个组头下（含一个没带「分行」字样的本体）",
              len(_st) == 1 and _st[0]["n"] == 3 and _st[0]["bn"] == 2,
              str([(g["company"], g["n"], g["bn"]) for g in _bg["groups"]]))
        check("机会库：组头的地点合并全部分行、截止时间取「最近一个还没过期的」",
              len(_st) == 1 and _st[0]["cities"] == ["北京", "苏州", "上海"] and _st[0]["dl"] == far,
              str((_st[0]["cities"], _st[0]["dl"] - far)) if _st else "?")
        check("机会库：多字主体词优先 —— 明月期货台州营业部归到「明月期货」而不是「明月期货台」",
              any(g["company"] == "明月期货" and g["n"] == 2 for g in _bg["groups"]) and
              not any("明月期货台" in g["company"] for g in _bg["groups"]),
              str([g["company"] for g in _bg["groups"]]))
        check("机会库：名字相近但不同家的没被并到一起（星河银行 / 星辰银行各是一家）",
              any(g["company"] == "星河银行" and g["n"] == 1 for g in _bg["groups"]))
        check("机会库：一家只有一条的不会被并进别家，也不会自己长出折叠壳",
              any(g["company"] == "红星教育" and g["n"] == 1 for g in _bg["groups"]))
        _bc = query_opportunities(obr, [], {"co": ["星辰银行"]})
        check("机会库：点组头的公司名，它下面 3 个分行一起筛出来",
              _bc["total"] == 1 and _bc["total_rows"] == 3,
              "%d 家 / %d 条" % (_bc["total"], _bc["total_rows"]))
        # 分页必须按「家」切：同一家的几条要么全在这一页、要么全在下一页。
        # 逐页翻一遍，每页里每个组都得是完整的（rows 条数 == n），四页加起来正好 4 家 7 条。
        _pgs = [query_opportunities(obr, [], {"sort": ["co"], "limit": ["10"],
                                             "offset": [str(i)]}) for i in range(5)]
        check("机会库：分页按「家」切 —— 每页里的组都是完整的（同一家的几条不会被切到两页）",
              all(len(g["rows"]) == g["n"] and len(g["rows"]) > 0
                  for p in _pgs for g in p["groups"]) and
              len(_pgs[0]["groups"]) == 4 and sum(g["n"] for g in _pgs[0]["groups"]) == 7 and
              len(_pgs[4]["groups"]) == 0,
              str([[(g["company"], g["n"]) for g in p["groups"]] for p in _pgs]))
        check("机会库：归并键跟着记录走（离线样例能在本机按同一个键归堆）",
              all(isinstance(r.get("gk"), str) and r["gk"] for r in obr))
        check("机会库：装载时就补好归并键（不是每次请求现算）",
              "opp_assign_keys(rows)" in src[src.find("def load_opportunities"):src.find("def opp_facets_cached")])
        check("机会库：页面按 groups 渲染（一家一行），不是按 rows",
              "OPP.groups.forEach" in html and "function oppGroupRow" in html and
              "const list = g.n <= 1 ? g.rows" in html and "tb.appendChild(oppRow(r, g.n > 1))" in html)
        check("机会库：主行整行可点展开、但点标签不算（交互元素要排除掉）",
              "const OPP_INTER" in html and "e.target.closest(OPP_INTER)" in html and
              'tr.className = "grp"' in html)
        check("机会库：主行/子行复用投递进度表那套样式（她不用重新学一套）",
              'tr.className = sub ? "sub" : ""' in html and 'class="subdot"' in html and
              'class="gtoggle"' in html and 'class="gmeta"' in html and
              "tr.grp .gmeta{" in html and "tr.sub td:first-child::before" in html)
        check("机会库：计数同时报「家」和「条」两个口径",
              "共 \" + OPP.all_groups + \" 家公司 · \" + OPP.all + \" 条岗位" in html and
              "筛出 \" + OPP.total + \" 家（\" + OPP.total_rows + \" 条岗位）" in html)
        check("机会库：页面上写清了「一家公司只占一行、点这一行摊开」",
              "一家公司只占一行" in html and "点这一行就摊开看每一条" in html)
        check("机会库：「再看 60」按家算", "再看 60 家" in html)
        check("机会库：离线样例也在本机归堆（键还是后端那份，不重抄规则）",
              "function oppGroupRows" in html and "OPP.total = groups.length" in html)
        f5 = opp_facets(orows)
        check("机会库：行业分面统计", ["金融业", 2] in f5["industries"], str(f5["industries"]))
        check("看板页有第三个标签：机会库",
              "机会库" in html and 'data-tab="opps"' in html)
        check("看板页有机会库表格和筛选",
              'id="oppTb"' in html and "投递入口" in html and 'id="oppOpen"' in html)
        check("看板页有含免笔试快筛", 'id="oppExam"' in html)
        check("看板页有「最近新开」快筛", 'id="oppNew"' in html)
        check("「最近新开」是循环档位", "OPP_FRESH_STEPS" in html and "oppNewLabel" in html)
        check("「最近新开」请求带 newdays", 'p.set("newdays"' in html)
        check("「最近新开」写进筛选记忆", "fresh: oppNew" in html)
        check("行内「新」徽标有样式", "oppFreshTag" in html and ".fresh{" in html)
        check("后端有更新时间戳字段 upd", "def parse_opp_date" in src and "day_start" in src)

        # —— v0.7.0：全字段、全可筛、包含/不包含、排序规则 ——
        check("机会库：公式列用空格分隔的城市也切开（源表两种写法混着来）",
              parse_opp_record({"fields": {
                  "公司名称": "X",
                  "地点②-筛地点选这里！！！！！！": "广州 深圳 上海"}})["cities"] == ["广州", "深圳", "上海"],
              str(parse_opp_record({"fields": {
                  "公司名称": "X", "地点②-筛地点选这里！！！！！！": "广州 深圳 上海"}})["cities"]))
        check("机会库：企业性质同义写法归并成一条",
              _opp_nature(["民营企业"]) == ["民企"] and _opp_nature(["私企"]) == ["民企"]
              and _opp_nature(["民企"]) == ["民企"] and _opp_nature(["中外合资/港澳台资"]) == ["外企/合资"]
              and _opp_nature(["股份/集体/混合/其他性质"]) == ["其他"],
              str([_opp_nature([x]) for x in ("民营企业", "私企", "中外合资/港澳台资", "股份/集体/混合/其他性质")]))
        r7 = parse_opp_record({"fields": {
            "公司名称": "国家电网", "企业性质": ["央国企"], "行业大类": ["能源/电力"],
            "地点②-筛地点选这里！！！！！！": "北京", "备注/提示": "国网校招一批",
            "招聘岗位": "电气工程师", "截止时间": "2026-11-30", "更新时间": "2026/09/01",
            "文本 7": ["需要笔试"]}})
        check("机会库：源表 14 个字段一个不落（含备注/提示、企业性质）",
              r7["note"] == "国网校招一批" and r7["nat"] == ["央国企"] and r7["nature"] == ["央国企"]
              and r7["id"] == r7["id"] and r7["updated"] == "2026/09/01"
              and r7["exam"] == ["需要笔试"] and r7["cities"] == ["北京"],
              "%s / %s / %s" % (r7["note"], r7["nat"], r7["cities"]))
        check("机会库：公司名里夹的 markdown 链接还原成文字",
              parse_opp_record({"fields": {
                  "公司名称": "小马智行[Pony.ai](http://Pony.ai)"}})["company"] == "小马智行Pony.ai",
              parse_opp_record({"fields": {
                  "公司名称": "小马智行[Pony.ai](http://Pony.ai)"}})["company"])
        check("机会库：源表里那几张「筛选前必看」的说明卡片不算岗位",
              parse_opp_record({"fields": {"公司名称": "[筛选前必看-点这里](https://x/y)"}}) is None)

        # 2026-09-28 起：包含(q) 与 不包含(qx) 两个框同时生效；qnot=1 是旧口径，仅兼容老链接
        check("机会库：旧的整框排除（qnot=1）仍然可用",
              query_opportunities(orows, oapp, {"q": ["某某"], "qnot": ["1"]})["total"] == 2)
        check("机会库：包含搜索多个词要全部命中（还能跨字段）",
              query_opportunities(orows, oapp, {"q": ["金融 广州"]})["total"] == 1)
        check("机会库：只要不含（出现就排掉）",
              query_opportunities(orows, oapp, {"qx": ["金融"]})["total"] == 1)
        _sit = query_opportunities(orows, oapp, {"q": ["金融"], "qx": ["深圳"]})
        check("机会库：包含 + 不含 同时生效（金融 里去掉 深圳）",
              _sit["total"] == 1 and _sit["rows"][0]["company"] == "某某科技",
              "%d 条 / %s" % (_sit["total"], [r["company"] for r in _sit["rows"]]))
        check("机会库：包含和不含互相抵消时结果为空",
              query_opportunities(orows, oapp, {"q": ["金融"], "qx": ["广州 深圳"]})["total"] == 0)
        check("机会库：不含也能命中非公司字段（备注 / 行业 / 岗位文本）",
              query_opportunities(orows, oapp, {"q": ["算法"], "qx": ["某某"]})["total"] == 2)

        # 2026-09-28 起：两个搜索框都能「先选字段再填词」（qf / qxf）
        check("机会库：字段限定——词只在该字段里找（行业里有金融的是 2 条）",
              query_opportunities(orows, oapp, {"q": ["金融"], "qf": ["ind"]})["total"] == 2)
        check("机会库：同一个词换个字段就找不到（公司名里没有金融）",
              query_opportunities(orows, oapp, {"q": ["金融"], "qf": ["co"]})["total"] == 0)
        check("机会库：字段限定对「不含」同样生效（城市里有深圳的只有 1 条）",
              query_opportunities(orows, oapp, {"qx": ["深圳"], "qxf": ["city"]})["total"] == 2)
        check("机会库：不含限定到公司名时排不掉任何人（三家名字里都没有深圳）",
              query_opportunities(orows, oapp, {"qx": ["深圳"], "qxf": ["co"]})["total"] == 3)
        _two = query_opportunities(orows, oapp,
                                   {"q": ["金融业"], "qf": ["ind"], "qx": ["广州"], "qxf": ["city"]})
        check("机会库：两边各自限定不同字段，一起生效（金融业 里去掉城市含广州的）",
              _two["total"] == 1 and _two["rows"][0]["company"] == "已截止公司",
              "%d 条 / %s" % (_two["total"], [r["company"] for r in _two["rows"]]))
        check("机会库：字段名认不出来就退回整条记录（老链接 / 手改 URL 不会筛空）",
              query_opportunities(orows, oapp, {"q": ["某某"], "qf": ["不存在的字段"]})["total"] == 1)
        check("机会库：限定字段后空格分词同样要全部命中（城市里同时有北京+苏州的只有 1 条）",
              query_opportunities(orows, oapp, {"q": ["苏州"], "qf": ["city"]})["total"] == 1 and
              query_opportunities(orows, oapp, {"q": ["北京 苏州"], "qf": ["city"]})["total"] == 1,
              "多值字段是拼成一串后再找词")
        check("机会库：选了字段但没填词 = 不筛（不能把结果筛没）",
              query_opportunities(orows, oapp, {"qf": ["co"], "qxf": ["city"]})["total"] == 3)
        check("机会库：字段下拉的取值表是干净的唯一键",
              len(OPP_SEARCH_FIELD_KEYS) == len(OPP_SEARCH_FIELDS) and "" in OPP_SEARCH_FIELD_KEYS)

        # v0.10.0：包含 / 不含 两块各能挂**多条**条件，每条各选各的字段
        _multi = query_opportunities(orows, oapp,
                                     {"q": ["金融业", "广州"], "qf": ["ind", "city"]})
        check("机会库：多条包含条件各查各的字段（行业金融业 且 城市广州）",
              _multi["total"] == 1 and _multi["rows"][0]["company"] == "某某科技",
              "%d 条 / %s" % (_multi["total"], [r["company"] for r in _multi["rows"]]))
        check("机会库：两个词配错字段就查不到（城市里找金融业、行业里找广州）",
              query_opportunities(orows, oapp,
                                  {"q": ["金融业", "广州"], "qf": ["city", "ind"]})["total"] == 0)
        check("机会库：多条不含条件命中任一条就排掉（城市含深圳 或 公司名含某某）",
              query_opportunities(orows, oapp, {"q": ["金融业"], "qf": ["ind"],
                                               "qx": ["深圳", "某某"],
                                               "qxf": ["city", "co"]})["total"] == 0,
              "两家各被一条条件排掉")
        _multi2 = query_opportunities(orows, oapp, {"q": ["金融业"], "qf": ["ind"],
                                                    "qx": ["深圳"], "qxf": ["city"]})
        check("机会库：只有一条不含命中时，另一家还在（对照上面那条）",
              _multi2["total"] == 1 and _multi2["rows"][0]["company"] == "某某科技",
              "%d 条 / %s" % (_multi2["total"], [r["company"] for r in _multi2["rows"]]))
        check("机会库：字段留空串 = 全部字段（页面发的就是空串，不能被当成「串了上一个字段」）",
              query_opportunities(orows, oapp, {"q": ["新能源", "宏图"],
                                               "qf": ["ind", ""]})["total"] == 1,
              "行业限定 + 整条兜底，两个都命中才留")
        check("机会库：字段数组比词数组短时，多出来的词在全部字段里找",
              query_opportunities(orows, oapp, {"q": ["新能源", "宏图"],
                                               "qf": ["ind"]})["total"] == 1)

        check("机会库：按公司名筛",
              query_opportunities(orows, oapp, {"co": ["某某科技"]})["total"] == 1)
        check("机会库：按企业性质（归并后）筛",
              query_opportunities(orows, oapp, {"nat": ["民企"]})["total"] == 3)
        all4 = orows + [r7]
        check("机会库：按行业排序", 
              query_opportunities(all4, oapp, {"sort": ["ind"], "limit": ["10"]})["rows"][0]["industries"][0] == "新能源")
        revl = query_opportunities(all4, oapp, {"sort": ["dl"], "rev": ["1"], "limit": ["10"]})["rows"]
        check("机会库：排序反序（截止最远的排最前，认不出日期的仍然沉底）",
              revl[0]["company"] == "宏图地产" and revl[-1]["company"] == "已截止公司",
              str([x["company"] for x in revl]))
        check("机会库：企业性质也进了分布统计", ["民企", 3] in f5["nature"], str(f5["nature"]))

        for probe, what in [('id="oppByNat"', "企业性质分布面板"),
                            ('id="incRows"', "「包含」条件行容器"), ('id="excRows"', "「不含」条件行容器"),
                            ('id="incAdd"', "加一条包含条件"), ('id="excAdd"', "加一条不含条件"),
                            ('class="qtag"', "包含 / 不含 两块的名字"),
                            ('class="qgroup"', "两块搜索条件各占一行的分组容器"),
                            ('id="oppNat"', "企业性质下拉"), ('id="oppSortInd"', "按行业排序"),
                            ('id="oppRev"', "排序反序按钮"), ("oppDetRow", "全字段详情行"),
                            ("data-det", "详情展开按钮"), ("fbext", "分布面板展开全部"),
                            ("fbin", "分布面板内搜索")]:
            check("看板页有" + what, probe in html)
        # 字段下拉由页面照注入的列表现生成，源头就是后端那份白名单 —— 只需确认真的注进去、没漏项
        _fj = re.search(r"const OPP_FIELD_OPTS = (\[.*?\]);", html, re.S)
        check("看板页的搜索字段列表来自后端白名单（不是在页里另抄一份）",
              bool(_fj) and json.loads(_fj.group(1)) == [list(x) for x in OPP_SEARCH_FIELDS],
              (_fj.group(1)[:110] + "…") if _fj else "没找到 OPP_FIELD_OPTS")
        check("看板页每条条件都把「字段 + 词」成对发出去（多个条件各查各的字段）",
              'p.append("q", c.v)' in html and 'p.append("qf", c.f)' in html and
              'p.append("qx", c.v)' in html and 'p.append("qxf", c.f)' in html)
        check("看板页搜索条件能加、能删（含离线自筛用同一套口径）",
              "function qRowsAdd" in html and "function qRowsRender" in html and
              'qRowsRender("inc"); qRowsRender("exc")' in html and
              "function oppIncOk" in html and "function oppExcHit" in html)
        check("看板页重建条件行后会把已填的词写回去（不然删一条就把别的输入也抹了）",
              "if(fill) row._inp.value = c.v;" in html and
              "qRowSync(row, c, kind, true)" in html and
              "qRowSync(row, row._item, pair[0], false)" in html)
        # 条件行互相压：两处都踩过，且都属于「自己看着没问题、窄一点才露出来」的那种。
        # ① .qrow 写 min-width:0 —— 宽度不够时行被压得比内容还窄，删除钮横向溢出去，
        #    盖住旁边那条的下拉框、最后一条还会盖住右边缘的 ＋。改成卡在 min-content。
        # ② 行内输入框的下限被上面那条带 id 的 `#pane-opps input[type=search]` 盖住
        #    （不带 id 的 `.qrow > input[type=search]` 根本没生效），按 190px 算，
        #    一行挂三条必然溢出。修法是给行内那条也带上 #pane-opps。
        check("看板页条件行宽度不够时往下换行、不会横向压到旁边那条",
              "flex:1 1 268px;min-width:min-content" in html and
              ".qrow{flex:1 1 268px;min-width:0" not in html)
        check("看板页条件行里的输入框下限没被带 id 的全局规则盖掉",
              "#pane-opps .qrow > input[type=search]" in html)
        # 通知表 / 面试进度「这一条对应哪家投递记录」必须走严配对 —— 松子串会把「京东」
        # 认成「京东方」，在通知表右侧凭空多挂一个「已投递」（2026-10-09 她贴截图的那两条）。
        # 机会库「我投过没」那种软提示仍用松的（那里宁可多认，不误伤）。
        check("看板页：通知 / 面试进度 → 投递记录 是严配对（京东方 ≠ 京东），机会库仍用松的",
              "function sameCompanyTight" in html and
              'APPS.filter(a => sameCompanyTight(a["公司名称"], n["公司名称"]))' in html and
              'APPS.filter(x => sameCompanyTight(x["公司名称"], it["公司"]))' in html and
              html.count('sameCompany(r.company || "", oppCo)') == 1 and
              html.count('sameCompany(r.company || "", n)') == 1)
        # 计数口径：默认就开着的「还没截止」也是筛选，标签不能自称「共全表 9167 条」
        # 而同屏的「筛出岗位」卡写 8104。按真实条数比就不会再漏算默认开着的筛选。
        check("看板页计数按真实条数比，不再靠列举筛选条件（漏算了默认开着的「还没截止」）",
              "const partial = OPP.seed ? (OPP.total_rows !== scopeN)" in html and
              "(OPP.total !== OPP.all_groups || OPP.total_rows !== OPP.all)" in html and
              '"筛出 " + OPP.total + " 家（" + OPP.total_rows + " 条岗位）"' in html)
        # 筛选条件记忆：三个标签页统一走 lsSave / lsLoad，且恢复都排在首次渲染之前。
        # 顺序反了 = 拿一屏默认条件把存档冲掉，这正是「刷新后筛选没了」最常见的成因。
        check("看板页三个标签页的搜索 / 筛选都记进 localStorage（刷新 / 重开浏览器不丢）",
              'const OPP_QKEY = "at.opps.q.v1"' in html and
              'const APP_QKEY = "at.apps.q.v1"' in html and
              'const NT_QKEY = "at.notices.q.v1"' in html and
              all(s in html for s in ("function lsSave", "function lsLoad",
                                      "function oppSave", "function oppLoad",
                                      "function appSave", "function appLoad",
                                      "function ntSave", "function ntLoad")))
        check("看板页三个标签页都是「先恢复、后渲染」（顺序反了筛选会被空条件冲掉）",
              html.find("oppLoad();") < html.find('qRowsRender("inc"); qRowsRender("exc");') and
              html.find("appLoad();") < html.find("function renderAll") and
              html.find("ntLoad();") < html.find("function renderAll") and
              html.find("function appSave") < html.find("function appLoad") and
              html.find("function ntSave") < html.find("function ntLoad"))
        # v0.27.0 起 renderApps 开头先挡一手「正在拖行就整个跳过」（见下面拖动并入那几条），
        # 所以看 appSave(); 的窗口从 120 放宽到 400 —— 不能删那几行守卫，只能跟着挪。
        check("看板页保存点挂在渲染函数里，且每个 save 都有 loaded 闸门（没恢复完不许写）",
              "appSave();" in html.split("function renderApps", 1)[1][:400] and
              "ntSave();" in html.split("function renderNotices", 1)[1][:120] and
              "oppSave();" in html.split("async function loadOpps", 1)[1][:200] and
              html.count("if(!oppQLoaded) return;") == 1 and
              html.count("if(!appQLoaded) return;") == 1 and
              html.count("if(!ntQLoaded) return;") == 1)
        check("看板页读回筛选时会验型 / 验白名单，坏存档只丢那一项",
              "OPP_FIELD_OPTS.some(o => o[0] === c.f)" in html and
              "OPP_SORTS.indexOf(d.sort) >= 0" in html and
              "APP_QUICKS.indexOf(d.quick) >= 0" in html and
              "ST_ORDER.indexOf(d.status) >= 0" in html and
              "NT_QUICKS.indexOf(d.quick) >= 0" in html and
              "NT_SORTS.indexOf(d.sort) >= 0" in html and
              'NOTICES.some(r => (r["通知类型"] || []).indexOf(d.type) >= 0)' in html)
        check("看板页浏览器存储被禁时能看出来（页脚有状态小标，不是静默吞掉）",
              "function memoChip" in html and "memoChip()" in html and
              'localStorage.setItem(LS_KEY_PROBE' in html and
              "本浏览器不记筛选" in html and "筛选条件已记住" in html)
        # 光有 span 不够 —— CSS 掉了它就只是个继承成灰色的字，跟没写一样（本文件就踩过：
        # 一条并行编辑把 .memo 规则吞了，而断言只查 HTML 不查样式，全绿却看不出问题）
        check("页脚状态小标的样式在（掉了就只剩一个没颜色的字）",
              "#foot .memo{" in html and "#foot .memo.bad{" in html and
              "var(--green)" in html.split("#foot .memo{", 1)[1][:200] and
              "var(--red)" in html.split("#foot .memo.bad{", 1)[1][:140])
        # 保存挂在渲染函数上，而渲染每 10 秒被自动刷新碰一次：不比对就会每 10 秒写一次，
        # 而且开两个标签页时会互相把对方的筛选覆盖掉。必须带内容去重。
        check("看板页重复保存同一份筛选会被跳过（不然两个标签页会互相覆盖）",
              "const _lsLast = {}" in html and
              "if(_lsLast[key] === s) return;" in html and
              "_lsLast[key] = s;" in html)
        # 「检查更新」页面发的是 POST，路由要是错放到 do_GET 里就是 404 —— 只看页面看不出来
        _pi = src.find("def do_POST")
        _nx = src.find("\nclass ThreadingServer", _pi)
        _post_src = src[_pi:_nx] if _pi >= 0 and _nx > _pi else ""
        check("看板页「检查更新」按钮走后端 POST 路由（不是错挂在 GET 上）",
              'path == "/api/opps-sync"' in _post_src and
              'fetch("/api/opps-sync", {method:"POST"' in html and
              "def sync_opportunities_bg" in src and "force=True" in _post_src)
        # 服务在跑就该自己保鲜：看门狗线程不能只指望页面被打开
        check("起服务时会启动机会库看门狗（不依赖页面被打开）",
              "def sync_watchdog" in src and
              "threading.Thread(target=sync_watchdog, args=(cfg,), daemon=True).start()" in src and
              "sync_opportunities_bg(cfg)" in src[src.find("def sync_watchdog"):
                                                  src.find("def opp_facets(")])
        check("看门狗认缓存文件的 mtime（重启后不会白拉九千条）",
              "opps_seed_cache(cfg)" in src[src.find("def sync_watchdog"):
                                            src.find("def opp_facets(")] and
              "opps_ok_recently(cfg)" in src[src.find("def sync_watchdog"):
                                             src.find("def opp_facets(")])
        check("看门狗自己出错也不会退出循环",
              "except Exception:" in src[src.find("def sync_watchdog"):
                                         src.find("def opp_facets(")])
        # 投递进度也得自己保鲜：飞书表改了状态，看板不该等你点刷新
        _wd = src[src.find("def sync_watchdog"):src.find("def opp_facets(")]
        check("看门狗也盯着投递进度的 TTL（不依赖页面被打开）",
              "sync_apps_bg(cfg)" in _wd and "apps_ok_recently(cfg)" in _wd and
              "cfg.apps_source" in _wd)
        check("看板页「同步飞书进度」按钮走后端 POST 路由（不是错挂在 GET 上）",
              'path == "/api/apps-sync"' in _post_src and
              'fetch("/api/apps-sync", {method:"POST"' in html and
              "def sync_apps_bg" in src and "force=True" in _post_src)
        # 页面文字好几千字，必须走 POST —— 挂 GET 上会被 http.server 的请求行长度上限挡掉
        # （扩展那一侧由 extension/test/popup-test.mjs 的 ⑮ 覆盖）
        check("扩展预填用的 /api/companies 挂在 POST 上（页面文字不能塞 URL）",
              'path == "/api/companies"' in _post_src and
              "def match_companies" in src and "def pick_company_hit" in src and
              "def known_companies" in src)
        check("看板页把「多久同步一次 / 上次几点同步的」写给用户看",
              'id="appSyncInfo"' in html and "META.apps_ttl_min" in html and
              "META.apps_sync_at" in html)
        check("离线样例里藏掉「同步飞书进度」（file:// 下点了必然失败）",
              'id="appSync"' in html and "sb.hidden = OFFLINE" in html)
        # 2026-09-30 踩过：给开源脱敏时把夹具/代码里的英文名做全局替换，
        # 「URLSearchParams」里的 Sea 被换成了别的词 → 机会库那段 JS 直接抛错、
        # 表格永远是空的，而自检照样全过（自检只看字符串在不在，不跑 JS）。
        # 这条守住几个必须原样出现的浏览器 API 名。
        check("模板里的浏览器 API 名没被替换坏（URLSearchParams 之类）",
              "new URLSearchParams" in html and
              all(b in html for b in ("addEventListener", "querySelectorAll",
                                      "getElementById", "JSON.parse")))
        # 2026-09-29 发现的旧毛病：.form 上是 display:flex，CSS 会盖掉 hidden 属性，
        # 于是「+ 手动加一条」那个开关一直失灵、表单常摊在页面上。
        check("「+ 手动加一条」那个表单默认收着（display:flex 会盖掉 hidden）",
              ".form[hidden]{display:none}" in html and 'id="addForm" hidden' in html)
        # 离线样例没法问后端，筛选必须在页面里做掉，否则双击打开那份文件里搜索框是死的
        check("看板页在离线样例里也能自己筛（不用后端）",
              "function oppSeedFilter" in html and "function oppFieldText" in html and
              "oppSeedFilter(OPP_SEED.rows)" in html)
        check("看板页离线样例会说明「只在这一屏快照里生效」", "这是离线样例页" in html)
        check("看板页离线打开时不再定时去 fetch（避免刷一串 CORS 报错）",
              'const OFFLINE = (location.protocol === "file:")' in html and
              "if(OFFLINE){" in html and "if(!OFFLINE) setInterval" in html)
        check("看板页离线打开时把「检查更新」藏起来（file:// 下点了必然失败）",
              'document.getElementById("oppSync").hidden = OFFLINE' in html)
        # 2026-10-01：飞书写入端是实时的（扩展一提交就写进去），这边 TTL 却 30 分钟 ——
        # 她投完立刻切过来看，表里还没有这条，看着就成了"没给我记录"。
        check("看板页打开 / 切回标签时顺手同步一次（不必等 30 分钟 TTL）",
              "async function nudgeApps" in html and "nudgeApps();" in html and
              'document.addEventListener("visibilitychange"' in html)
        check("顺手同步带节流（45 秒内刚同步过就不重复拉 lark-cli）",
              "const NUDGE_MIN = 45" in html and
              "age >= 0 && age < NUDGE_MIN" in html and "nudgeBusy" in html)
        check("后端把「距上次同步多少秒」和「上次记了什么」给页面",
              '"apps_age_s"' in src and "META.apps_age_s" in html and
              '"apps_changes"' in src and "META.apps_changes" in html)
        check("刚同步进来的新记录页面会报一句（否则 138 行里根本看不出来）",
              "function sayNewApps" in html and "sayNewApps(APPS.length - beforeN)" in html)
        check("看板页页脚会区分「离线样例」和「自动刷新」",
              "离线样例</b>（没有本地服务，不自动刷新）" in html)
        # 旧的那个「包含/不包含」二选一切换按钮必须已经拿掉，否则两个搜索框的语义会打架
        check("看板页不再有二选一的包含/不包含切换按钮", 'id="oppQNot"' not in html)

        # 顺序断言：restoreTab() 必须在 const OPP 之后调用。
        # 2026-09-28 踩过这个坑：它原来是立即执行的 IIFE，带 #opps 打开/刷新时
        # switchTab → ensureOpps() 去读 OPP，撞暂时性死区抛
        # "Cannot access 'OPP' before initialization"，机会库整页 JS 停摆。
        # 静态语法检查抓不到，只有真渲染才看得见 —— 所以这里把顺序钉死。
        _i_decl = html.find("const OPP =")
        _i_call = html.rfind("restoreTab();")
        check("标签页还原在 const OPP 之后才调用（防 TDZ 崩页）",
              _i_decl > 0 and _i_call > _i_decl and "function restoreTab" in html,
              "const OPP = @%d, restoreTab() @%d" % (_i_decl, _i_call))

        # 离线样例：把机会库快照嵌进去，免得离线打开空着；起服务时页面不带快照（省体积）
        os.makedirs(os.path.dirname(cfg.opps_json), exist_ok=True)
        with open(cfg.opps_json, "w", encoding="utf-8") as f:
            json.dump({"synced_at": "2026-01-01 00:00", "count": len(orows),
                       "rows": orows}, f, ensure_ascii=False)
        _OPPS_CACHE.update({"path": "", "mtime": 0.0, "rows": []})
        seeded = render_html(cfg, seed_opps=True)
        check("离线样例嵌入了机会库快照",
              "OPP_SEED = null" not in seeded and "宏图地产" in seeded)
        live = render_html(cfg)
        check("起服务时页面不嵌快照（数据走 /api/opps）",
              "OPP_SEED = null" in live and "宏图地产" not in live)

    # ---- 2026-09-30 的三处改动：备注可编辑 / 备注列不再被窄屏藏 / 紧迫度面板瘦身 ----
    check("接口 /api/status 兼管改备注（note 分支）",
          "APP_EDIT_FIELDS" in src and '"note": "备注"' in src)
    # ---- 2026-10-04 v0.23.0：表格整行可改 + 进度能回退 ----
    check("看板页表格里公司名 / 岗位名 / 投递时间 / 链接都能点着改（整行可改）",
          "const APP_CELLS = {" in html and
          'col:"公司名称", api:"company"' in html and
          'col:"岗位名称", api:"position"' in html and
          'col:"投递时间", api:"applied_at"' in html and
          'function inlineEdit(td, opts){' in html and
          'function saveAppField(id, api, value, label, old){' in html and
          'class="lnkedit"' in html and 'td input.cellin' in html)
    check("后来加的这几个可编辑格子在「整行可点展开」的免打扰名单里（主行加了东西也不会被误吞）",
          'const INTER = "a,select,input,textarea,.noteview,.cellv,.lnkedit,.del,.mg";' in html and
          'tr.querySelectorAll(".cellv[data-f]")' in html)
    check("公司名字段不许清空（空了 CSV 读出来会跳过这条，看着就像丢了数据）",
          'if "公司名称" in fields and not fields["公司名称"]:' in src and
          '"公司名称不能为空"' in src)
    check("进度视图：已经过的环节点一下能**退回来**（不是只能往前推）",
          'box.querySelectorAll(".ivn.cur,.ivn.next,.ivn.ok")' in html and
          'const back = el.classList.contains("ok");' in html and
          '"退回「"' in html and '.ivn.ok:hover{' in html)
    check("进度视图里那句「怎么点」写在页面上（光靠 hover 提示她不会知道）",
          'ivNote' in html and "点已经过的环节 = 退回来" in html)
    # ---- 2026-10-04 v0.23.1：就地编辑不许把表格挤变形 ----
    # 三条都是实测踩出来的：改一格牵连整张表重排，肉眼看像「把表弄坏了」。
    # 只取 `.cellv{...}` 这条规则本身来查 —— 注释里会提到踩过的那个写法，直接搜全文会误判。
    _cellv_rule = re.search(r"\.cellv\{[^}]*\}", html)
    _cellv_css = _cellv_rule.group(0) if _cellv_rule else ""
    check("可编辑格子的文字用 overflow-wrap 断行（`word-break:break-word` 会把最小内容宽度压成 1 个字，"
          "「岗位」列被挤成 84px、岗位名一字一行）",
          "overflow-wrap:break-word" in _cellv_css and "word-break" not in _cellv_css)
    check("就地编辑的浮层是**浮在格子上**的（绝对定位 + 原内容 visibility:hidden 继续占位），"
          "不是把格子内容换成 width:100% 的输入框",
          "td.editing{position:relative}" in html and
          "td.editing > *:not(.cellpop){visibility:hidden}" in html and
          "td .cellpop{position:absolute;" in html and
          'td.textContent = "";' not in html)
    # ---- 2026-10-05 v0.24.2：投递时间那格「一个个手打」 ----
    # 她截图框着「＋时间」问「改时间方式能不能方便一点」—— 那格是空的就退化成纯文本框，
    # 只能从零敲。现在空值也上日期选择器 + 一排「今天 / 昨天 / 前天」一点即存。
    check("投递时间格子有「今天 / 昨天 / 前天」三个快捷按钮，点一下直接存（不用点日历、不用敲键盘）",
          "function dayStr(off){" in html and '"cellquick"' in html and
          '["今天", 0], ["昨天", -1], ["前天", -2]' in html and
          'opts.save(dayStr(Number(b.dataset.off)) + " " + keep)' in html)
    check("投递时间那格原来是空的就**预填今天**（老写法是「空值 → 退回 text 输入框」，只能从零手打）",
          'const isTime = opts.kind === "when" && (!old || /^\\d{4}-\\d{2}-\\d{2}/.test(old));' in html and
          'inp.value = dayStr(0) + "T00:00";' in html)
    check("快捷按钮只改日期、保留原值的钟点（`2026-09-29 15:41` 不会被一键抹成 `00:00`）",
          'const keep = (hm && hm !== "00:00") ? hm : "00:00";' in html)
    check("点快捷按钮不让 blur 那条路再写一次（点按钮时 input 先失焦，会重发一遍）",
          "// 别让 blur 那条路再写一次" in html and "cancelled = true;" in html)
    check("链接那格的编辑入口是第二行的淡淡文字（跟「＋备注」一个样式），不跟「打开」胶囊抢同一行",
          'class="haslink"' in html and ">改链接</button>" in html and
          "td.haslink button.lnkedit{display:block" in html and
          "button.lnkedit{padding:0;border:none;background:none;" in html)
    # 备注是最早能改的一格，一直是「清空格子 + 塞输入框」那套 —— 备注列只有 72px，
    # 而那个输入框 `min-width:130px`，一点编辑整张表被撑到 1131px（总宽 +81）并横向溢出。
    # v0.23.1 起跟别的格子共用同一个浮框，所以这里钉的是「委托」而不是老那套 `.notein`。
    check("看板页备注点一下就地编辑，且走的是同一个浮框（不再自备一套会把表格撑宽的输入框）",
          "function editNote(el, r){" in html and 'class="noteview"' in html and
          'inlineEdit(el.closest("td"), {' in html and "notein" not in html)
    check("窄屏不再藏备注列（她屏幕 1080px，藏了等于没这功能）",
          "#pane-apps th:nth-child(8)" not in html)
    # ---- 2026-10-08 无障碍 A1：可点操作必须是原生 <button>（SPEC AC-17/18）----
    # 为什么要钉死：这三种原来都是 <span> 挂 onclick —— 鼠标能点，键盘 Tab 不到、回车触发不了，
    # 读屏也不会说它是按钮。改 <button> 之后焦点 / Enter / Space / 「按钮」角色全部白拿。
    # ⚠️ 反过来也一样要钉死：**不要**给几百行 <tr> 贴 tabindex=0 来「修」这个 ——
    # 那会让键盘用户按几百次 Tab 才到底部，比不可达还难受。整行点击的展开入口
    # 行内本来就有 .gtoggle 按钮可用，所以只断言「按钮在」，不断言「行有 tabindex」。
    check("无障碍 A1：备注 / 公司名 / 岗位名 / 投递时间这四种可点文字都是原生 <button>（不再是挂 onclick 的 <span>）",
          '<button type="button" class="cellv"' in html and
          '<button type="button" class="noteview"' in html and
          '<span class="cellv"' not in html and '<span class="noteview"' not in html)
    check("无障碍 A1：面试进度条的环节（推进 / 当前 / 退回）都是原生 <button>，键盘能 Tab 进去回车触发",
          '<button type="button" class="ivn ok"' in html and
          '<button type="button" class="ivn next"' in html and
          '<span class="ivn' not in html)
    check("无障碍 A1：整行点击只是**补充**入口，行内那个 .gtoggle 按钮才是键盘用户的路 —— "
          "所以行不许带 tabindex（贴了就是几百次 Tab）",
          '<button class="gtoggle" aria-expanded=' in html and
          'tr.setAttribute("tabindex"' not in html and 'tr.tabIndex' not in html)
    check("无障碍 A1：没有全局键盘监听抢 Enter —— 展开走的是原生按钮 click（Enter 在按钮上天然触发），"
          "真要加全局监听必须先判 e.target",
          "document.onkeydown" not in html and
          'document.addEventListener("keydown"' not in html and
          'window.addEventListener("keydown"' not in html)
    # ---- 2026-10-08 无障碍第二波 A2/A4/A5/A10（SPEC AC-17~20）----
    # A2 为什么用 aria-label 而不是加可见文字：删除那格 27px 宽，1080px 下整表横向零余量
    # （本文件另一条自检钉死了「并排加两个字必然出滚动条」），可见文字塞不进去；
    # × 留给鼠标用户，读屏的名字换成词 —— × 这个符号会被念成「乘号」。
    check("无障碍 A2：两个删除按钮（行删除 × / 筛选条件 ×）的可达名是词不是符号",
          'aria-label="删掉这条"' in html and
          'del.setAttribute("aria-label", "删掉这条条件")' in src)
    # A4：A1 那波给两处 .gtoggle 挂了 aria-expanded，全库查漏后还有四处展开开关，一样要挂
    check("无障碍 A4：详情展开（detbtn）/ 分布面板展开全部（fbext）/ 面试编辑器（ivedit）/ 展开全部（appOpenAll）都挂 aria-expanded",
          '(OPP_DET[rid] ? "true" : "false")' in html and
          '(st.open ? "true" : "false")' in html and
          '(ivEditKey === it["key"] ? "true" : "false")' in html and
          'oa.setAttribute("aria-expanded", appOpenAll ? "true" : "false")' in html)
    # A5：状态要能被读屏主动念出来 —— 不然改完了、同步完了，读屏用户根本不知道发生了什么
    check("无障碍 A5：toast / 飞书同步状态 / 机会库同步告警 / 邮箱状态条四个状态区挂 aria-live=polite",
          'id="toast" aria-live="polite"' in html and
          'id="appSyncInfo" aria-live="polite"' in html and
          'id="oppAlert" aria-live="polite"' in html and
          'aria-live="polite" hidden' in html)
    check("无障碍 A5：四个计数区（投递 / 通知 / 面试 / 机会库）挂 role=status，筛选后条数变化读屏会报",
          'id="appCount" role="status"' in html and 'id="ntCount" role="status"' in html and
          'id="ivCount" role="status"' in html and 'id="oppCount" role="status"' in html)
    # A10：跳转链接全是原生 <a href>，Tab 天然可达 —— 钉死「不许出现没有 href 的 <a>」
    # 和「不许用 location.href 在 span 上自造链接」，这两条一破键盘就丢了
    check("无障碍 A10：所有跳转链接都是原生 <a href>（Tab 可聚焦），没有 href 空壳或 JS 自造链接",
          html.count("<a ") == html.count("<a href=") and "location.href" not in html)
    # ---- 2026-10-08 无障碍第三波 A6/A7/A8/A9/A11/A12（SPEC AC-17~20）----
    # 逐项核过：六项在当前代码里**全部已达标**（审计清单是旧扫描的账）。不造轮子，
    # 但要用自检把现状钉死 —— 这些全是「顺手一改就没了」的东西，一破键盘/读屏跟着破。
    check("无障碍 A6/A7/A8：lang=zh-CN、title 有名字、viewport 不锁缩放（老人机放大是硬需求）",
          '<html lang="zh-CN">' in html and "<title>秋招看板</title>" in html and
          'content="width=device-width, initial-scale=1"' in html and
          "maximum-scale" not in html and "user-scalable=no" not in html)
    check("无障碍 A9：恰好一个 h1（页名），面板用 h2 —— 读屏按标题层级导航有骨架",
          "<h1>" in html and html.count("<h1") == 1)
    check("无障碍 A11：「并入 / 拆出」是原生 <button> 带实词（不是挂 onclick 的符号）",
          '<button class="mg' in html and "拆出" in html)
    check("无障碍 A12：系统开「减弱动态效果」时一律不动；全库没有 @keyframes / 无限循环动画可关",
          "(prefers-reduced-motion:reduce)" in html and
          "animation-iteration-count:1!important" in html and
          "@keyframes" not in html and "infinite" not in html)
    # 数据根目录默认是「脚本所在目录」，所以想拿别处的数据（演示数据）跑必须显式给 --base。
    # 2026-10-02 的教训：光 cd 到演示目录没用，读到的还是仓库里的真实数据 ——
    # 结果真实公司名被拍进了要开源的 samples/ 截图（好在没提交，已还原）。
    check("CLI 有 --base（换数据根目录）",
          'ap.add_argument("--base"' in src and
          "Config(cfg_path, base=(os.path.abspath(args.base) if args.base else None))" in src and
          "cfg_path = resolve_config_path(args.base, args.config)" in src)
    # 同一天的第二个教训（更凶）：--base 指过去、但 cwd 里也有 config.ini 时，
    # 老逻辑读的是 cwd 那份 → 演示服务拿真实飞书 token 把真实投递记录同步进了演示目录，
    # 截图里出现了真实公司名。所以 base 必须压过 cwd。
    _tmpc = tempfile.mkdtemp()
    try:
        _democfg = os.path.join(_tmpc, "config.ini")
        with open(_democfg, "w", encoding="utf-8") as f:
            f.write("[applications]\nsource = off\n")
        check("--base 里的 config.ini 优先于当前目录那份（config.ini 是真实 token 时尤其要命）",
              os.path.abspath(resolve_config_path(_tmpc, "config.ini")) == os.path.abspath(_democfg) and
              resolve_config_path(_tmpc, "nope.ini") == "nope.ini" and
              resolve_config_path("", "config.ini") == "config.ini")
    finally:
        shutil.rmtree(_tmpc, ignore_errors=True)

    _tmpb = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(_tmpb, "data"))
        with open(os.path.join(_tmpb, "data", "applications.csv"), "w", encoding="utf-8-sig") as f:
            f.write(",".join(CSV_FIELDS) + "\n")
        _cb = Config(None, base=_tmpb)
        check("--base 生效：数据目录、CSV 路径都跟着 base 走，不是脚本所在目录",
              os.path.abspath(_cb.out_dir) == os.path.abspath(os.path.join(_tmpb, "data")) and
              os.path.abspath(_cb.app_csv) == os.path.abspath(os.path.join(_tmpb, "data", "applications.csv")),
              "%s / %s" % (_cb.out_dir, _cb.app_csv))
    finally:
        shutil.rmtree(_tmpb, ignore_errors=True)

    # ---- 同一家公司投多个岗位：折到一块，计数按公司算 ----
    # 她的原话：「同一个公司投递多个岗位的话，在同一个公司下面增加子记录归纳到一块，
    # 总投递按照公司数计算」。这几条钉的就是这个口径。
    check("看板页同一家公司折成一组，多岗位才有主行（只投一个岗位的不折，省一次点击）",
          "function buildGroups" in html and "function appNodeRow" in html and
          'if(g.rows.length === 1){ tb.appendChild(appRow(g.rows[0], false, "", g)); return; }' in html and
          'tr.className = "grp" + (lvl === 2 ? " lvl2" : "") + (open ? " open" : "")' in html)
    check("看板页「总投递」按行数算（归并后主公司算一行），岗位数只做副标（不再被岗位数灌水）",
          "const allNodes = buildNodes(APPS);" in html and
          "const nco = allNodes.length;" in html and
          'kpi(nco, "总投递", "t-total", "all", false, APPS.length + " 个岗位")' in html and
          'document.getElementById("tabApps").textContent = nco;' in html)
    check("看板页其余 KPI / 状态分布 / 概览也都按行算（跟总投递同口径，加起来刚好等于行数），"
          "归并前后两个口径都报（涉及公司 = 源表原始家数）",
          'const cnt = grp => allNodes.filter(n => n.group === grp).length;' in html and
          "allNodes.forEach(n => { sc[n.best] = (sc[n.best]||0) + 1; });" in html and
          '"涉及公司 " + rawCo + " 家" + (mergedN ? "（归并后 " + nco + " 行）" : "")' in html and
          '"已收到通知的公司 " + withNotice + " / " + nco + " 家"' in html and
          'const rawCo = buildGroups(APPS).length;' in html and
          'const mergedN = mergedSubs(allNodes);' in html)
    # 她 2026-10-09 要的：把投递进度页那块「概览」换成近 7 天趋势图。
    # 她 2026-10-10 又收了一轮：「趋势图大一点，不要下面的文字，可以留最近一次投递和近 7 天投递数量」——
    # 柱子改成撑满面板（不再写死 62px），图下面只留两行。
    # ⚠️ 被砍掉的那几个数**不是删掉，是挪进 `#appOverview` 的 `title`**（鼠标停上去照旧看得见）——
    # 所以上面那条断言改成了认 title 里的字符串，**它必须继续存在**；这两条是一对，别只改一条。
    check("看板页「近 7 天投递」趋势图：7 根柱子按**本地**年月日对齐（用 UTC 切会把凌晨投的算到前一天）、"
          "今天在最右、柱子撑满面板、图底下只留「近 7 天投递数量」和「最近一次投递」两行，"
          "落不进这一周的和其余概览都挂在 title 里",
          '<h2>近 7 天投递</h2><div id="appTrend"></div>' in html and
          "function trendChart" in html and
          "for(let i = 6; i >= 0; i--){" in html and
          "const slot = tDays.find(x => x.t === Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));" in html and
          '(i === last ? " today" : "")' in html and
          'trendChart(document.getElementById("appTrend"), tDays);' in html and
          'kv("近 7 天投递数量", tTot + " 家")' in html and
          'kv("最近一次投递", last ? (last.label + " · " + String(last.lastAt).slice(5, 10)) : "—")' in html and
          '"另有 " + tRest + " 家不在这一周内（更早投的，或者还没填投递时间）"' in html and
          "ovEl.title = [" in html and
          ".panel.trend #appTrend{flex:1" in html and
          ".tchart{flex:1;display:flex;gap:7px;align-items:stretch;min-height:" in html and
          # 柱子高度这个旋钮**只许往上调**：110 以下就退回改之前那个「小图」了
          # （`flex:1` 只在旁边两块更高时才有富余可分，砍完内容后它自己是靠 min-height 顶起来的）。
          int(html.split(".tchart{flex:1;display:flex;gap:7px;align-items:stretch;min-height:")[1]
              .split("px")[0]) >= 140)
    check("看板页公司进度取组里最靠后的状态，且「已结束」不压过 Offer",
          "function bestOf" in html and 'm["已结束"] = 0;' in html and
          'g.group = appGroup({"状态": g.best});' in html)
    check("看板页公司名归一后再归组（「恒瑞达机械」和「深圳市恒瑞达机械有限公司」要是一家）",
          "function coNorm" in html and "const k = coNorm(raw) || NO_CO;" in html and
          "(股份有限公司|有限责任公司|集团有限公司|控股集团|有限公司|股份公司|分公司|集团|控股|公司)$" in html and
          "^(中国|[\\u4e00-\\u9fa5]{2,3}省|[\\u4e00-\\u9fa5]{2,3}市)" in html)
    check("看板页一家公司的邮箱通知按组汇总（条数相加 / 类型并集 / 截止取最近的）",
          "function envMerge" in html and "count += e.count;" in html and
          "next: isNaN(bestT) ? late : best" in html)
    check("看板页折叠展开记进 localStorage，搜索时自动摊开（不然看不出命中的是哪个岗位）",
          'const APP_OKEY = "at.apps.open.v1"' in html and
          "function appOpenOf" in html and "appOpenLoad();" in html and
          "const open = appOpenOf(g.key) || spread;" in html and
          "const open = appOpenOf(n.key) || spread;" in html and
          "lsSave(APP_OKEY, {all: !!appOpenAll, keys: keys});" in html)
    check("看板页主行 / 子行的样式齐备（掉了就只剩两行一样平的表格，看不出归属）",
          "tr.grp td{" in html and '"grp" + (open ? " open" : "")' in html and
          'tr.className = "grp" + (lvl === 2 ? " lvl2" : "") + (open ? " open" : "")' in html and
          "tr.grp.lvl2 .gtoggle" in html and "tr.sub .subco" in html and
          "tr.sub td:first-child::before" in html and 'class="subdot"' in html and
          "tr.grp.open .gtoggle .arw{transform:rotate(90deg)}" in html)
    check("看板页「展开全部 / 收起全部」按钮只在真有公司折叠时才出现",
          'id="appOpenAll"' in html and 'oa.hidden = !folded;' in html and
          'document.getElementById("appOpenAll").onclick' in html)
    # 她的原话：「展开的方式也太不方便了，要去点这个小三角才能展开，需要点这一行就能展开」
    # 最后那条 `not in` 是防双触发的：handler 绑在 tr 上，小三角的 click 会冒泡过来，
    # 谁要是再给 .gtoggle 单独绑一次，点三角就会切两次 = 看着像没反应。
    check("看板页公司主行整行可点展开（不用非得去戳那个 10px 的小三角）",
          "tr.grp{cursor:pointer}" in html and
          'const INTER = "a,select,input,textarea,.noteview,.cellv,.lnkedit,.del,.mg";' in html and
          "tr.onclick = e => {" in html and
          "if(e.target && e.target.closest && e.target.closest(INTER)) return;" in html and
          'tr.querySelector(".gtoggle").onclick' not in html and
          "tbody tr.grp:hover .gtoggle" in html)
    # ---- 2026-10-07 子公司并入主公司（投递进度表那个「并入」）----
    # 她截图框着 示例五局 / 示例南洋 / 示例国际城市开发：「把子公司归并到主公司下面」。
    # 关键决定：**程序不猜谁是子公司** —— 「中国」开头的大公司一堆（四大行、邮储、
    # 兵器、航发这类互不相干的），按前缀一并就全糊成一家；「示例家居」（男装）跟「示例家居集团」（卫浴）
    # 更是长得比真母子还像。只认她在页面上点过的那条。
    # 前几条是**真跑一遍读写**（不是断字符串）—— 文件坏了 / 空串 / 并到自己身上都得对。
    _parent_dir = tempfile.mkdtemp(prefix="at-parent-")
    try:
        _pc = Config(None, base=_parent_dir)
        check("并入表：一开始没这个文件 → 读到空的，也不白建一个",
              load_app_parents(_pc) == {} and not os.path.exists(app_parent_path(_pc)))
        set_app_parent(_pc, "示例五局", "示例建筑集团")
        set_app_parent(_pc, "示例南洋", "示例建筑集团")
        check("并入表：写进去能原样读回来，落在 data/app_parents.json",
              load_app_parents(_pc) == {"示例五局": "示例建筑集团", "示例南洋": "示例建筑集团"} and
              os.path.basename(app_parent_path(_pc)) == "app_parents.json",
              str(load_app_parents(_pc)))
        set_app_parent(_pc, "TCL实业", "")
        check("并入表：空串 = 明确「不并了」，能盖住内置打底那条（不能被当成没写）",
              load_app_parents(_pc).get("TCL实业") == "" and APP_PARENT_BUILTIN.get("TCL实业") == "TCL")
        set_app_parent(_pc, "示例五局", "示例五局")
        check("并入表：并到自己身上 = 没并（不留一条噪音）",
              "示例五局" not in load_app_parents(_pc))
        with open(app_parent_path(_pc), "w", encoding="utf-8") as _f:
            _f.write("{坏文件")
        check("并入表：文件坏了当没有（看板不能因为一个坏文件打不开）",
              load_app_parents(_pc) == {})
        with open(app_parent_path(_pc), "w", encoding="utf-8") as _f:
            _f.write('{" 示例五局 ": " 示例建筑集团 ", "TCL实业": ""}')
        check("并入表：名字去首尾空白，空串照样留着（它是有意义的值）",
              load_app_parents(_pc) == {"示例五局": "示例建筑集团", "TCL实业": ""},
              str(load_app_parents(_pc)))
    finally:
        shutil.rmtree(_parent_dir, ignore_errors=True)

    check("并入表跟 /api/board 一起发给页面（页面不自己读文件），打底表也一起发下去",
          '"app_parents": load_app_parents(cfg),' in src and
          '"app_parent_builtin": dict(APP_PARENT_BUILTIN),' in src and
          "let APP_PARENT = DATA.app_parents || {};" in html and
          "if(d.app_parents) APP_PARENT = d.app_parents;" in html)
    check("并入是个写接口：/api/app-parent 收 child + parent，缺 child 报 400",
          'path == "/api/app-parent"' in _post_src and
          'set_app_parent(self.cfg, child, str(body.get("parent", "")))' in _post_src and
          '"缺少 child"' in _post_src)
    check("页面：一个公司一条「并入」按钮；已并过的显示「拆出」，点一下拆回来",
          "function mergeBtnHtml" in html and
          "const p = parentLabelOf(comp.key, comp.name);" in html and
          '(p ? "拆出" : "并入")' in html and "data-mgparent=" in html)
    check("页面：主公司按她点过的那条往上追（支持 A→B→C），最多 20 跳防她自己并成环",
          "function rootOfComp" in html and "for(let i = 0; i < 20; i++)" in html and
          "if(seen.indexOf(nk) >= 0) break;" in html and 'const PKEY = "@";' in html)
    check("页面：查主公司先认她点过的、再认内置打底；空串要认出来（不能用 || 顺手吞掉）",
          "const books = [APP_PARENT, APP_PARENT_BUILTIN];" in html and
          "Object.prototype.hasOwnProperty.call(book, k)" in html)
    check("页面：主公司自己也有记录就拉进来当「自己那家」—— 不许冒出一行同名（内置打底那条最容易撞）",
          "eaten.indexOf(x) < 0 && coNorm(x.comps[0].name) === k" in html and
          "if(eaten.length) out = out.filter(n => eaten.indexOf(n) < 0);" in html)
    check("页面：两层 —— 主行写「N 家子公司 · N 个岗位」+ 子公司名；只投一个岗位的子公司直接铺一条（不套壳）",
          '(n.subs.length + " 家子公司 · " + n.rows.length + " 个岗位")' in html and
          "const shorts = n.merged ? n.subs.map(c => c.name) : posed;" in html and
          "if(c.rows.length === 1){ tb.appendChild(appRow(c.rows[0], true, c.name, c)); return; }" in html and
          "n.merged = n.wantMerge && n.subs.length > 0;" in html)
    check("页面：归并组里的子行写得清是哪家子公司，且不许把公司那一列撑宽（限宽 + 省略号）",
          "function subLabel(name)" in html and 'class="subco"' in html and
          "tr.sub .subco{display:inline-block;vertical-align:middle;max-width:190px;" in html)
    check("页面：并入面板给候选（她表里现有的公司名），也能直接打一个新的（「示例建筑集团」她表里没有）",
          'kind: "merge", options: names' in html and
          'inp.setAttribute("list", dl.id);' in html and
          'dl.id = "coNames" + (++cellDlSeq);' in html)
    check("页面：框里清空 = 什么都不改（要取消得点「不并了」），免得点走就把归并抹了",
          'save: v => { if(!v){ renderApps(); return; } saveAppParent(child, name, v, old); }' in html and
          '(opts.clear || opts.save)("")' in html and "不并了" in html)
    check("页面：并入按钮竖着堆在 × 上面 —— 这张表在 1080px 下横向零余量，并排加两个字必然出滚动条",
          "td.c-op{white-space:nowrap}" in html and "button.mg{display:block;" in html and
          "button.mg.on{color:var(--muted)}" in html)
    check("页面：点「并入 / 拆出」不许顺带把整行展开（一次委托 + stopPropagation）",
          'document.getElementById("appTb").addEventListener("click"' in html and
          'e.target.closest("button.mg")' in html and "e.stopPropagation();" in html)

    # ---- 2026-10-07 之二：拖动并入（她：「并入方法太麻烦了，能不能拖动并入」）----
    check("页面：把一家公司那一行拖到另一家那一行上就并过去 —— 四个事件都挂在表上",
          'const appTbEl = document.getElementById("appTb");' in html and
          'appTbEl.addEventListener("dragstart"' in html and
          'appTbEl.addEventListener("dragover"' in html and
          'appTbEl.addEventListener("drop"' in html and
          'appTbEl.addEventListener("dragend"' in html)
    _dov = html.split('appTbEl.addEventListener("dragover"', 1)[1].split(
        'appTbEl.addEventListener("dragleave"', 1)[0]
    check("页面：dragover 里必须 preventDefault —— 少了它浏览器根本不让 drop（表现为「怎么拖都没反应」）",
          "e.preventDefault();" in _dov and "dropEffect" in _dov and
          "dragLast.classList.remove(\"dropok\")" in _dov)
    check("页面：只有「代表一家公司」的行能拖，岗位子行不许拖（拖它没有任何意义）",
          html.count("tr.draggable = true;") == 2 and
          "tr.dataset.drgKey = comp.key;" in html and "tr.dataset.drgName = comp.name;" in html)
    check("页面：从输入框 / 下拉 / 按钮上按下不算拖这一行（否则她编辑到一半整行会被拖走）",
          'const DRAG_NO = "input,textarea,select,.cellpop,.cellin,button,a";' in html and
          "e.target.closest(DRAG_NO)" in html and "e.preventDefault(); return;" in html)
    check("页面：拖到自己身上 / 拖到自己的子孙下面要拦住（不然并出一个环）",
          "function chainHas(startLabel, who)" in html and
          "if(chainHas(parent, c.name)){" in html and
          "if(c && tr && tr !== c.row) dropMerge(c, tr);" in html)
    check("页面：正在拖的那会儿不许重建表格（10 秒一次的轮询会换掉被拖那行、当场作废这次拖）",
          "if(dragChild) return;" in html.split("function renderApps", 1)[1][:300])
    check("页面：拖完那一下的 click 要吞掉（不然会顺带把落点那行展开 / 收起）",
          "if(Date.now() - dragEndAt < 300) return;" in html)
    check("页面：拖完把主公司那条链上的行都摊开（不然被拖那行不见了，主公司在哪儿得自己找）",
          "function openAlongChain(name)" in html and
          "appOpenKeys[PKEY + coNorm(p)] = true;" in html and
          "openAlongChain(parent);" in html)
    check("页面：拖动状态用 var 不用 let（appNodeRow 运行时就要读 dragEndAt，"
          "而 renderAll 可能在这段代码之后才第一次跑 —— let 会撞 TDZ 直接报错）",
          "var dragChild = null, dragLast = null, dragEndAt = 0;" in html)
    check("页面：拖动源 / 落点的视觉反馈都在（落点描一圈、拖动源淡下去），且高亮压得过表行底色",
          'tr[draggable="true"]{cursor:grab}' in html and
          "tr.dragging{opacity:.45}" in html and
          "tr.dropok>td{background:var(--accent-soft)!important;" in html)
    check("页面：「并入」按钮的 title 里也提了能拖（她不一定看表格下面那段说明）",
          "也可以直接把这一行拖到另一家公司那一行上" in html)
    check("页面：拖动并入那条通道最终写的还是同一个接口（不许自己另造一套落盘）",
          "saveAppParent(c.key, c.name, parent, old);" in html and
          html.count('fetch("/api/app-parent"') == 1)
    check("页面：表格下面写清了拖动并入 / 「并入 / 拆出」怎么用，以及程序不猜",
          "同一家的几个子公司" in html and "只认你自己点过 / 拖过的那条，程序不猜" in html and
          "把一家那一行直接拖到" in html and "另一家那一行上，松手就并过去" in html and
          "点「拆出」" in html)

    check("「已投 N 天」并进了投递时间那一格，列数没多",
          html.count('<th scope="col">投递时间</th>') == 1 and '<th scope="col">已投</th>' not in html)

    # ---- 2026-10-02：手动切深色 ----
    # 深色 token 只留一份、注入两处，是这套东西不跑偏的前提：
    # 抄第二遍的话，改浅色忘了改深色，深色下就会冒出一个刺眼的白块，而且没人会发现。
    check("看板页深色配色只有一份 token，系统偏好 / 手动切两个入口都注入了",
          "__DARK__" not in html and
          html.count("--bg:#0e1013") == 2 and DARK_TOKENS.count("--bg:#0e1013") == 1 and
          "@media (prefers-color-scheme:dark){:root:not([data-theme=light]){" in html and
          ":root[data-theme=dark]{" in html)
    # 这段脚本晚于样式表跑的话，刷新时会先闪一屏浅色再跳深色 —— 只有真的在 <head> 里才行。
    _i_bs = html.find('localStorage.getItem("at.theme.v1")')
    _i_css = html.find("<style>")
    check("看板页配色在样式表之前就定下来（放后面刷新会先闪一屏浅色）",
          _i_bs > 0 and _i_css > _i_bs and html.find("data-theme-mode") > 0,
          "bootstrap @%d, <style> @%d" % (_i_bs, _i_css))
    # 三态而不是两态：她的 Mac 本身可能就在深色下，没「自动」就没法回到跟随系统。
    # 「自动」时不能写死解析结果，否则系统白天黑夜自动切换时页面不跟。
    check("看板页配色三态（自动 / 浅色 / 深色），选完记住、刷新回来还是它",
          'id="themeBar"' in html and html.count('data-thm="') == 3 and
          'const THEME_KEY = "at.theme.v1"' in html and
          "lsSave(THEME_KEY, {mode: mode});" in html and
          'r.removeAttribute("data-theme")' in html and
          'document.documentElement.getAttribute("data-theme-mode") || "auto"' in html)
    # 深色下强调色是浅蓝，白字压上去对比度不够（WCAG AA）。收进 token 才不会被漏掉 ——
    # 原来靠一条 @media 只盖了主按钮，切深色时别的实心块（.dtag、链接悬停）依然是白字。
    check("实心强调色块上的字色走 --on-accent token（深色下自动换深字）",
          "--on-accent:#fff;" in html and "--on-accent:#0e1013" in html and
          html.count("color:var(--on-accent)") >= 4 and "color:#fff" not in html)
    check("原生控件也跟着主题走（color-scheme），不然深色下拉框 / 滚动条会冒出白块",
          "color-scheme:light" in html and "color-scheme:dark" in html)
    # 2026-10-03 晚（v0.22.0）：她嫌顶部乱、重复 ——「截止紧迫度」面板（四档里常常三个是 0、
    # 第四个就是「已处理」）和「处理进度」面板（那个百分比跟「已处理 N」是同一个数）都砍了，
    # 只留唯一有信息量的「通知类型分布」。反着钉一条，别哪天又长回来。
    check("顶部只剩「通知类型分布」一块面板，另两块（截止紧迫度 / 处理进度）已经砍掉",
          'id="ntByType"' in html and 'id="ntByUrg"' not in html and 'id="ntOverview"' not in html and
          "const NT_PANEL = [" not in html)
    check("顶部数字砍到 3 张（总通知数 / 还没处理 / 已处理），今天截止·3天内·逾期并进副标",
          'kpi(n, "总通知数"' in html and 'kpi(open.length, "还没处理"' in html and
          'kpi(doneN, "已处理"' in html and '" · 3 天内 "' in html and
          'kpi(todayOpen, "今天截止"' not in html and 'kpi(overOpen, "逾期未处理"' not in html)
    check("面板瘦身没带坏表格的 7 档分组（一周内那几档还在表里）",
          "NT_ORDER" in html and "这一周内" in html and "没识别出截止" in html)

    # ---- 2026-10-02：邮箱看板加了「面试进度」（每家公司一条进度条）----
    # 她的原话：「能看到收到测评、面试的每个公司现在的投递进度条，进度条上节点信息不固定，
    # 但一般是测评、AI面试、一面、二面、终面、offer……还没到的环节一般显示为『面试-offer』（灰色）」。
    # 这几条钉的就是这套口径。
    check("面试进度：默认阶梯就是她说的那六环、顺序不跑偏（offer 永远最后）",
          INTERVIEW_LADDER == ["测评", "AI面试", "一面", "二面", "终面", "offer"] and
          INTERVIEW_ORDER[-1] == "offer" and "测评" in INTERVIEW_ORDER and "笔试" in INTERVIEW_ORDER)
    check("面试进度：轮次优先从邮件主题里认（通知类型只有「面试」两个字，认不出第几轮）",
          interview_stage_of({"通知类型": ["面试"], "邮件主题": "邀请你参加校招初试"}) == ("一面", False) and
          interview_stage_of({"通知类型": ["面试"], "邮件主题": "一面前问卷更新邀请"}) == ("一面", False) and
          interview_stage_of({"通知类型": ["面试"], "邮件主题": "复试安排通知"}) == ("二面", False) and
          interview_stage_of({"通知类型": ["面试"], "邮件主题": "终面邀请"}) == ("终面", False) and
          interview_stage_of({"通知类型": ["面试"], "邮件主题": "AI面试邀请"}) == ("AI面试", False) and
          interview_stage_of({"通知类型": ["测评"], "邮件主题": "请参加某集团的在线测评"}) == ("测评", False) and
          interview_stage_of({"通知类型": ["笔试"], "邮件主题": "邀请你参加在线笔试"}) == ("笔试", False))
    # 主题什么都没说时，只能记成「一面」但**必须打上待确认** —— 猜高了会让进度条骗人
    check("面试进度：主题没说第几轮就标「待确认」（不能瞎猜成一面还装得像真的）",
          interview_stage_of({"通知类型": ["面试"], "邮件主题": "来自某某的面试邀请"}) == ("一面", True) and
          "ivn.cur.ask" in html and 'it["待确认"]' in html)
    _pi2 = src.find("def do_POST")
    _nx2 = src.find("\nclass ThreadingServer", _pi2)
    _post2 = src[_pi2:_nx2] if _pi2 >= 0 and _nx2 > _pi2 else ""
    check("面试进度：写接口挂在 POST /api/interview 上（点一下就要落盘）",
          'path == "/api/interview"' in _post2 and "set_interview_progress" in _post2)
    # ---- 2026-10-03 晚（v0.22.0）：这一页改成**两个可切换的视图** ----
    # 她的原话：「还是在这一页做两个可切换的看板吧。一个像之前一样专门显示各种面试测评截止时间，
    # 另外一个拿来显示投递进度条（测评的不用显示）。然后把邮箱通知这个看板整页优化一下，
    # 顶页太多没用重复的东西了」。
    # 她上一轮（v0.21.0）要的是「并进表里一列」，看完实物又改主意了 —— 照新的做就是。
    check("这一页是两个视图，切换靠 <html data-ntv>（不重新渲染，切过去不闪、不空白）",
          'id="ntvDl"' in html and 'id="ntvIv"' in html and
          'id="ntDlView"' in html and 'id="ntIvView"' in html and
          'html[data-ntv="dl"] #ntIvView,html[data-ntv="iv"] #ntDlView{display:none}' in html and
          'localStorage.getItem("at.ntv.v1")' in html)
    check("在哪个视图是 <head> 里就定好的（晚一步会先闪一下另一个视图）",
          html.find('localStorage.getItem("at.ntv.v1")') < html.find("<style>"))
    # 一家 6~8 环，全铺出来一行装不下；她自己也说了「没到的显示成一个灰块」
    check("还没到的环节折成一个灰块（不是一环一环铺开），止步的连灰块都不画",
          "if(i > ci + 1 || (stop && i > ci)) return;" in html and "rest[rest.length - 1]" in html)
    # 她这轮的「测评的不用显示」——指的是只停在测评阶段的那几家整条不列
    check("进度视图：只停在「测评」阶段的公司整条不列",
          "const IV_DROP = /^测评/;" in html and "function ivShown(it){" in html and
          "return !IV_DROP.test(cur);" in html)
    check("进度视图：按公司一家一行（不再按通知行把同一根进度条重复画好几遍）",
          "function renderIvView(){" in html and 'id="ivList"' in html and
          "function ivShowList(){" in html)
    check("进度视图里点环节能改当前；改 / 止步 / 恢复自动都在，编辑器铺在公司那行下面",
          'box.querySelectorAll(".ivn.cur,.ivn.next,.ivn.ok")' in html and "ivEditor(it)" in html and
          ".ivstop" in html and ".ivreset" in html and ".ivsave" in html)
    # ---- v0.24.0：这一页也能筛进度（她 2026-10-05 提的）----
    check("进度视图有筛选条：搜公司 / 环节标签 / 清除筛选 / 计数",
          'id="ivQ"' in html and 'id="ivNodeChips"' in html and 'id="ivReset"' in html and
          'id="ivCount"' in html)
    check("进度筛选（选的环节 + 搜索词）记在这台浏览器里，换页回来还在",
          'const IV_FKEY = "at.ivf.v1";' in html and "function ivSave(){" in html and
          "function ivLoad(){" in html and "ivLoad();" in html)
    check("环节按钮按后端给的标准次序排（中文按字典序会把「一面」排到「测评」前面；"
          "认不出的环节兜到最后，不能因为 indexOf == -1 顶到最前）",
          "const IV_ORDER = META.iv_order" in html and "const ivOrderOf = n =>" in html and
          "nodes.sort((a, b) => ivOrderOf(a) - ivOrderOf(b));" in html)
    check("筛出来的条数写在筛选条右边（不然她不知道是被筛掉了还是真没有）",
          "筛出 " in html and "共 " in html and 'ivNodeChips' in html)
    check("选中的环节以后不存在了要回落成「全部」（别打开就是一张空列表）",
          "if(ivNode !== IV_ALL && ivNode !== IV_STOP && nodes.indexOf(ivNode) < 0) ivNode = IV_ALL;" in html)
    # 她这轮的「示例涂料公司已经给我挂了」→ 一键止步
    check("每行都有「止步」按钮，一点就标（原来是「改」→「止步于此」两跳）",
          'class="ivbtn ivhalt"' in html and 'box.querySelectorAll(".ivhalt")' in html and
          "button.ivhalt:hover{color:var(--red)" in html)
    check("止步只发 stop、不带 nodes/cur —— 不会把这家变「手动改过」，当前环节照旧自动跟",
          'ivPost({key: it["key"], company: it["公司"], stop: stop}' in html)
    check("止步来源是「已结束」的行不给「继续」按钮，只写一行「投递表已结束」",
          'class="ivhaltnote"' in html and "投递表已结束" in html and
          'it["止步来源"] === "已结束"' in html)
    # 挂了的必须看得见 —— 原来「只停在测评就整条不列」会连它在测评阶段挂掉一起藏掉
    check("挂了的公司不受「只停在测评就不列」那条约束（否则测评阶段挂掉的会凭空消失）",
          'if(it["止步"]) return true;' in html)
    check("止步的行看得出来：整行浅底、环节链条淡下去",
          ".ivrow.stopped{background:var(--line2)}" in html and
          ".ivrow.stopped .ivn,.ivrow.stopped .ivl{opacity:.62}" in html and
          ".ivrow.stopped .ivbar{opacity" not in html)
    # 她这轮的「已结束的在进度条后面加一个已挂，红色底色」
    check("挂了（止步）的行，进度条末尾挂一个红底「已挂」标",
          'class="ivdead"' in html and ">已挂</span>" in html and
          "if(stop){" in html and "background:var(--red);color:var(--on-red)" in html and
          'it["止步来源"] === "已结束"' in html)
    check("红标的字色走 --on-red token（浅色深红底白字 / 深色浅红底深字），红标不被「止步就淡下去」一起淡掉",
          "--on-red:#fff;" in html and "--on-red:#260d0b" in html and
          ".ivrow.stopped .ivdead" not in html)
    check("筛选后没公司时的空态跟「真没公司」分开写（不然她会以为数据没了）",
          "这个筛选下没有公司" in html and "还没有进到面试的公司" in html)
    _nt_tb = html.find('id="ntTb"')
    _nt_head = html.rfind("<thead>", 0, _nt_tb) if _nt_tb > 0 else -1
    _nt_head_blk = html[_nt_head:_nt_tb] if _nt_head > 0 else ""
    check("通知表回到 9 列（面试进度那一列拿掉了），表头 / 分段 / 空表三处列数一致",
          _nt_head_blk.count('<th scope="col">') == 9 and "面试进度" not in _nt_head_blk and
          '<td colspan="9">' in html and 'colspan="9" class="empty"' in html,
          "表头 %d 列" % _nt_head_blk.count('<th scope="col">'))
    # ---- 2026-10-08 无障碍 A3（语义标签）：表头要能让读屏说出「这是哪一列」----
    # 三张表的表头全部带 scope="col"；「操作」那两列表头本来就是空的，光有 scope 读屏还是念不出
    # 这是干嘛的，所以补一个 aria-label。上面那条「9 列」守卫也顺势改成数带 scope 的表头 ——
    # 列数守卫照旧生效（少一列 / 多一列照样红），只是数的东西换成了真正发给读屏的那个。
    check("无障碍 A3：三张表的表头都带 scope=\"col\"（读屏能报出列名）—— 7+9+8 共 24 个有字的，"
          "外加 2 个空的见下一条",
          html.count('<th scope="col">') == 24)
    check("无障碍 A3：那两列没有文字的表头（操作列）补了 aria-label，读屏不会念成「空」",
          html.count('<th scope="col" aria-label="操作"></th>') == 2)
    # 她这轮提的第一件事
    check("已经处理掉的通知不再报「过期 N 天」，只画一个破折号（**截止还没到的除外**，见下条）",
          'const txt = (done && !chk) ? "—"' in html and 'td.dl-off{color:var(--faint)}' in html and
          '((done && !chk) ? " dl-off" : "")' in html)
    check("顶部那条「待办」提示条和 secttl 小标题都砍了（信息并进视图切换条右边那句）",
          'id="ntAlert"' not in html and 'id="ntSub"' not in html and 'id="ntvHint"' in html)
    check("「只看紧急的」这个能力留着（提示条砍了，入口挪到下面那排按钮里）",
          'id="ntUrgent"' in html and 'ntQuick === "urgent"' in html)
    # ---- 2026-10-08 「同公司归纳，可展开」（她拍板的默认形态）----
    check("通知页「按公司归纳」：切换按钮在、默认开；公司行展开入口是原生 <button> 挂 aria-expanded，"
          "展开状态记内存（NT_GRP_OPEN），翻页/重渲染不丢",
          'id="ntGrouped"' in html and "let ntGrouped = true" in html and
          '<button class="ntexp" aria-expanded=' in html and "NT_GRP_OPEN" in html)
    check("通知页平铺形态还在（关掉归纳 = 一行一封 + 紧急度分段），两种形态都不许删",
          'tr0.className = "sect g-" + g' in html and "detailRow(r, false)" in html and
          "detailRow(r, true, !isOpen)" in html)
    # ⚠️ 2026-10-09 抓到的真 bug：flex 直接挂在 `<td colspan="9">` 上 → 该格子**退出表格布局**、
    # colspan 失效，宽度塌成第一列那点（实测 219px / 表宽 1050px），公司名被挤成「一字一行」的竖条。
    # 表格是 table-layout:fixed，归纳行必须保持 display:table-cell，flex 挂内层 div（.ntcor）。
    # 这条只钉字符串，「真的横跨整行」由 board-check ⑥d 量宽度（肉眼看很容易当成设计如此）。
    check("通知页归纳行：flex 挂在内层 div 上，td 不许自己 flex（挂了 colspan 就失效，公司名挤成竖条）",
          '<td colspan="9"><div class="ntcor">' in html and
          "tr.ntco td>.ntcor{display:flex" in html and
          "tr.ntco td{display:flex" not in html)
    # ⚠️ 同一个坑的另一半（2026-10-09 量归纳行时顺手抓到的**老**bug）：平铺形态的分段行
    # `tbody tr.sect td` 一直就是 `display:flex` —— 实测宽度 **219px / 表宽 1050px**，
    # 「今天截止 / 3 天内截止 / 已经处理掉的」这些标题被压成窄窄一条。同一条铁律，一起钉死。
    check("通知页分段行：flex 也挂内层 div（.sectr），td 保持 table-cell（否则标题只有 219px 宽）",
          '<td colspan="9"><div class="sectr">' in html and
          "tr.sect td>.sectr{display:flex" in html and
          "tr.sect td{display:flex" not in html)
    # 她 2026-10-09 报「你又给我漏邮件了」的真因之一：归纳形态**一条分段标题都没有**，
    # 40 家公司平铺，「已经处理掉的」只靠灰字区分，那条正沉在第 37/40 位。
    # 补一条只**加**一行的分隔条 —— 不藏、不淡任何东西（她那两次否掉的都是「藏起来/淡下去」）。
    check("通知页归纳形态：「已经处理掉的」有一条分隔条（只加行，不藏、不淡任何数据）",
          "已经处理掉的" in html and 'trd.className = "sect g-done"' in html and
          'NT_CLOSED.map(esc).join(" / ")' in html)
    check("通知页归纳形态：分隔条上的家数/条数两个口径都报（不然她数不出来）",
          "const dGrp = doneKeys.length;" in html and
          "const dCnt = list.filter(r => ntDone(r) && !ntOpenDone(r)).length" in html and
          "let doneSectShown = false, checkSectShown = false" in html and
          "if(allDone && !doneSectShown){" in html)
    # 段头必须和它底下的行是**同一堆**。第一版是「遇到第一家 check 就插段头」，靠 groups 自带的排序
    # 兜底 —— 但一家既有「待处理」又有「已完成未到期」的公司（她表里就有这么一家）排在纯 check 公司前面，
    # 段头被提前插到它头上，紧跟其后的「没识别出截止」那两家眼睛一看就落在
    # 「标了已完成、但截止还没到」标题底下。所以改成先分三堆再渲染。
    check("通知页归纳形态：分段按「堆」渲染（段头一插，底下的行就一定属于它）",
          "const checkKeys = groups.filter(k => gmap[k].some(r => ntOpenDone(r)))" in html and
          "const doneKeys = groups.filter(k => !isChk(k) && gmap[k].every(r => ntDone(r)));" in html and
          "const order = groups.filter(k => !isChk(k) && !isDn(k)).concat(checkKeys, doneKeys);" in html and
          "order.forEach(k => {" in html and "groups.forEach(k => {" not in html)
    # ⚠️ 2026-10-09 她连着两次报「你又给我漏邮件了」——两次都落在**同一条缝**里：
    # 她自己把通知标成「已完成」，于是那行既不计入「还没处理」，又整行灰字沉底；
    # 可它的**截止时间根本还没到**（当天 21:00 截止的笔试通知、次日 10:21 截止的测评通知）。
    # 「已完成」在她那儿更像「我看过了」，不等于「时间上这件事结束了」。这一组是正面修复的钉子。
    check("「标了已完成、但截止还没到」被单独判成一段（ntOpenDone 只认「已了结 + 截止存在且还没过」）",
          "function ntOpenDone(r){" in html and
          'return d !== null && !isOverDl(r["通知截止时间"]);' in html and
          'if(ntDone(r)) return ntOpenDone(r) ? "check" : "done";' in html)
    check("那一段有名字、有颜色、有左色条（不是灰色的「已经处理掉的」一个待遇）",
          '["check",   "标了已完成、但截止还没到", "var(--orange)"]' in html and
          "tbody tr.sect.g-check td{box-shadow:inset 3px 0 0 var(--orange)}" in html and
          'trc.className = "sect g-check"' in html)
    check("那一段还是「只加行」：照常报剩余时间、整行不灰、也不吃 dl-off 的淡字",
          'chk ? (d === 0 ? "今天截止" : "还有 " + d + " 天")' in html and
          '((done && !chk) ? "done " : "")' in html and
          '((done && !chk) ? " dl-off" : "")' in html)
    check("那一段的家数/条数两个口径都报，且排在「已经处理掉的」之前",
          "const cGrp = checkKeys.length;" in html and
          "checkKeys.reduce((s, x) => s + gmap[x].filter(r => ntOpenDone(r)).length, 0)" in html and
          "isCheck && !checkSectShown" in html and
          html.index('["check",') < html.index('["done",'))
    check("「已处理」那张卡和视图切换条都报出这撮数（她两次都是从这两个地方开始找表的）",
          'const checkN = NOTICES.filter(r => ntOpenDone(r)).length;' in html and
          '" · 其中 " + checkN + " 条截止还没到"' in html and
          '" 条标了已完成但截止还没到"' in html)
    # 「筛出 0」必须说清是**哪几个条件**把它筛没的 + 就地清除。
    # 她这次的现场就是：类型标签是持久化的（localStorage），她点过的「资料补充」一直挂着，
    # 搜某家公司名当场 0 条 —— 而那条恰恰是「测评」。
    check("通知页筛出 0 条：把生效的筛选条件一条条念出来 + 就地「清除全部筛选」",
          "function ntConds(){" in html and "function ntClearFilters(){" in html and
          "当前筛着 " in html and 'id="ntClearEmpty"' in html and
          "const _cds = ntConds();" in html and
          "ntQuick = \"all\"; ntType = null; ntQ = \"\"; ntHideDone = false;" in html)
    check("通知页筛选条件口径只有一份（空态与「清除筛选」按钮共用 ntConds，不许两处各写一套）",
          html.count("function ntConds(){") == 1 and
          html.count("function ntClearFilters(){") == 1 and
          "const nConds = ntConds();" in html and
          'ntResetEl.textContent = nConds.length > 1 ? "清除筛选（" + nConds.length + " 项）"' in html)
    # 反着钉一条历史痕迹：v0.20.1 那套互斥切换（`data-ntview` / `ntViewBar` / `ivJumpDl`）是
    # 「把她已有的一块藏起来」的翻车，拆了就不许再长回来。v0.22.0 这次是她自己看了实物要的切换，
    # 所以 `data-ntv` 是允许的 —— 但老那套名字如果回来了，就说明有人又按老思路写了一遍。
    check("v0.20.1 那套互斥切换的痕迹（data-ntview / ntViewBar / ivJumpDl）没留半截",
          "data-ntview" not in html and "ntViewBar" not in html and "NTVIEW_KEY" not in html and
          'id="ivJumpDl"' not in html and "ivScrollToNotices" not in html)
    check("截止通知那排筛选一件不少：搜索 / 类型标签 / 排序 / 隐藏已处理 / 只看没处理的",
          'id="ntQ"' in html and 'id="ntTypeChips"' in html and 'id="ntReset"' in html and
          'id="ntSortUrg"' in html and 'id="ntSortDl"' in html and 'id="ntSortCo"' in html and
          'id="ntHideDone"' in html and 'id="ntOnlyTodo"' in html and 'id="ntCount"' in html)

    _tmpi = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(_tmpi, "data"))
        _ci = Config(None, base=_tmpi)

        def _n(co, types, subj, when="2026-09-20 10:00", pos="管理培训生"):
            return {"公司名称": co, "岗位名称": pos, "通知类型": types, "邮件主题": subj,
                    "通知收到时间": when, "邮件收到时间": when, "通知截止时间": ""}

        _nts = [_n("长河银行", ["测评"], "长河银行在线测评邀请"),
                _n("长河银行", ["AI面"], "长河银行AI面试邀请", "2026-09-25 10:00")]
        _iv = derive_interview(_ci, _nts, [])
        check("面试进度：按公司把多条通知揉成一条进度，当前 = 走过的最靠后那一环",
              len(_iv) == 1 and _iv[0]["当前"] == "AI面试" and _iv[0]["节点"] == INTERVIEW_LADDER
              and _iv[0]["通知数"] == 2 and _iv[0]["来源"] == "自动" and _iv[0]["待确认"] is False)

        _nts2 = _nts + [_n("长河银行", ["面试"], "长河银行二面通知", "2026-09-28 10:00")]
        _iv2 = derive_interview(_ci, _nts2, [])
        check("面试进度：主题写了「二面」就认二面（不是笼统记成一面）",
              _iv2[0]["当前"] == "二面" and _iv2[0]["待确认"] is False)

        set_interview_progress(_ci, "长河银行", company="长河银行",
                               nodes=["测评", "一面", "offer"], cur="一面")
        _iv3 = derive_interview(_ci, _nts2, [])
        check("面试进度：手改过的以手改为准（她删掉「AI面试」就是这家没有，不许自作聪明加回来）",
              _iv3[0]["节点"] == ["测评", "一面", "offer"] and _iv3[0]["当前"] == "一面"
              and _iv3[0]["来源"] == "手动")
        set_interview_progress(_ci, "长河银行", stop=True)
        check("面试进度：止步标记存得住、也读得回来",
              load_interview_progress(_ci)["长河银行"].get("止步") is True
              and derive_interview(_ci, _nts2, [])[0]["止步"] is True)
        set_interview_progress(_ci, "长河银行", reset=True)
        check("面试进度：能一键恢复自动（手动记录删干净，回到按邮件推断）",
              load_interview_progress(_ci) == {}
              and derive_interview(_ci, _nts2, [])[0]["当前"] == "二面")

        # 环节顺序：她填什么顺序都行，落盘前统一按标准次序摆好，认不出的环节留在 offer 前面
        set_interview_progress(_ci, "长河银行",
                               nodes=["offer", "一面", "某某环节", "测评", "群面"])
        check("面试进度：环节统一按标准次序排，认不出的环节不丢、压在 offer 前面",
              load_interview_progress(_ci)["长河银行"]["节点"] ==
              ["测评", "群面", "一面", "某某环节", "offer"],
              str(load_interview_progress(_ci).get("长河银行", {}).get("节点")))
        _snapshot = load_interview_progress(_ci)
        check("面试进度：环节全空不许写（不然进度条会变成一条空线）",
              set_interview_progress(_ci, "空环节公司", nodes=["", "   "]) is False
              and load_interview_progress(_ci) == _snapshot)
        check("面试进度：一家公司最多 12 个环节（手滑粘一整段进去也不至于把页面撑爆）",
              len(_iv_clean_nodes(["n%d" % i for i in range(40)])) == INTERVIEW_MAX_NODES)

        with open(interview_path(_ci), "w", encoding="utf-8") as _f:
            _f.write("{ 这不是 json")
        check("面试进度：文件坏了当没有（看板不能因为一个坏文件打不开）",
              load_interview_progress(_ci) == {} and len(derive_interview(_ci, _nts2, [])) == 1)

        _iv5 = derive_interview(_ci, [],
                                [{"公司名称": "星桥科技", "岗位名称": "产品运营",
                                  "状态": "面试", "投递时间": "2026-09-01 10:00"}])
        check("面试进度：只收到过邮件的公司进来，只有投递状态的公司也进来（状态「面试」至少算走到一面）",
              len(_iv5) == 1 and _iv5[0]["当前"] == "一面" and _iv5[0]["通知数"] == 0)
        check("面试进度：只发过「资料补充」这类通知的公司不进这块（没有环节可谈）",
              derive_interview(_ci, [_n("云溪智能", ["资料补充"], "补充材料通知")], []) == [])
        check("面试进度：还在简历筛选阶段（只投了）的公司不占地方",
              derive_interview(_ci, [],
                               [{"公司名称": "星桥科技", "状态": "已投递",
                                 "投递时间": "2026-09-01 10:00"}]) == [])

        # ---- v0.24.0：投递表里这家**全部记录**都标了「已结束」→ 进度条自动算止步 ----
        # 她平时只在投递表标一次（飞书里写的「已挂」也映射成它），进度条这边就该跟着沉下去。
        def _one(rows):
            hit = [r for r in rows if r["公司"] == "长河银行"]
            return hit[0] if hit else {}
        _lh = _one(derive_interview(_ci, _nts2, [
            {"公司名称": "长河银行", "状态": "已结束", "投递时间": "2026-09-01 10:00"}]))
        check("面试进度：投递表里这家全标了「已结束」→ 自动按止步算（只在一个地方标就够）",
              _lh.get("止步") is True and _lh.get("止步来源") == "已结束", str(_lh))
        # 反例：一家两个岗位，挂了一个、另一个还在面 —— 不能整家算止步（示例文具公司就是这情况）
        _lh2 = _one(derive_interview(_ci, _nts2, [
            {"公司名称": "长河银行", "状态": "已结束", "投递时间": "2026-09-01 10:00"},
            {"公司名称": "长河银行", "状态": "面试", "投递时间": "2026-09-01 10:00"}]))
        check("面试进度：一家多岗只挂了一个岗，不整家算止步（示例文具公司就是这情况，别一刀切）",
              _lh2.get("止步") is False, str(_lh2))
        check("面试进度：手点的止步和「已结束」推出来的止步都读得回来，来源分得清",
              _lh.get("止步来源") == "已结束")
        # 为什么那一行只写「投递表已结束」、不给「继续」按钮？就因为它按不动 ——
        # 手工把 stop 置回 false 也没用，止步的源头在投递表。钉住这条，
        # 免得以后有人顺手加个「继续」，她点了没反应又来问。
        set_interview_progress(_ci, "长河银行", company="长河银行", stop=False)
        _lh3 = _one(derive_interview(_ci, _nts2, [
            {"公司名称": "长河银行", "状态": "已结束", "投递时间": "2026-09-01 10:00"}]))
        check("面试进度：投递表已结束 → 手工取消失不掉止步（所以那一行不给「继续」按钮）",
              _lh3.get("止步") is True, str(_lh3))
        set_interview_progress(_ci, "长河银行", reset=True)
    finally:
        shutil.rmtree(_tmpi, ignore_errors=True)

    # 演示数据生成器（tools/make-demo-data.py）必须引用主程序的字段清单，不能各抄一份。
    # 2026-09-30 踩过：v0.15.0 给 applications.csv 加了「飞书状态」列，生成器里那份手抄的
    # 表头没跟上 → samples/投递记录样例.csv 少一列，而 README 写的字段是带「飞书状态」的。
    # 抄出来的副本迟早会漂，所以这里把「引用」和「落盘结果」两头都钉住。
    _root = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(_root, "tools", "make-demo-data.py"), encoding="utf-8") as _f:
            _gen_src = _f.read()
    except OSError:
        _gen_src = ""
    check("演示数据生成器引用主程序的字段清单（不另抄一份）",
          "applytracker.CSV_FIELDS" in _gen_src)
    _sample = os.path.join(_root, "samples", "投递记录样例.csv")
    if os.path.exists(_sample):
        with open(_sample, encoding="utf-8-sig", newline="") as _f:
            _hdr = next(csv.reader(_f), [])
        check("samples/投递记录样例.csv 的表头 == CSV_FIELDS", _hdr == CSV_FIELDS,
              "样例表头 %r" % (_hdr,))

    # ---- v0.29.0：邮箱直连（IMAP）识别规则 + 公司匹配 + 数据源接线（ADR-001 / ADR-002） ----
    _tm = tempfile.mkdtemp(prefix="at-mail-")
    try:
        _mb = os.path.join(_tm, "base")
        os.makedirs(os.path.join(_mb, "data"), exist_ok=True)
        # 一份投递记录当「用户名单」（公司名故意写成工商全称，考验剥后缀的比对）
        with open(os.path.join(_mb, "data", "applications.csv"), "w",
                  encoding="utf-8-sig", newline="") as _f:
            _w = csv.DictWriter(_f, fieldnames=CSV_FIELDS)
            _w.writeheader()
            _w.writerow({"公司名称": "长河银行股份有限公司", "岗位名称": "管培生",
                         "状态": "已投递", "投递时间": "2026-09-01 10:00"})
        # 配了邮箱 → [notices] source=auto 应走到 imap
        _cp = os.path.join(_tm, "config.ini")
        with open(_cp, "w", encoding="utf-8") as _f:
            _f.write("[notices]\nsource = auto\n\n[mail]\n; 这是一条注释\nsource = auto\n"
                     "host = imap.qq.com\nport = 993\nuser = me@qq.com\nauthcode = SECRET\n")
        _mc = Config(_cp, base=_mb)
        check("邮箱配置：配了 [mail] user+authcode → [notices] source=auto 走邮箱直连",
              _mc.notices_source == "imap" and _mc.mail_enabled
              and _mc.notices_csv == os.path.join(_mb, "data", "notices.csv"))
        check("邮箱配置：authcode 只从配置读，不写进别处（也不外发）",
              _mc.mail_authcode == "SECRET")
        _cp_off = os.path.join(_tm, "off.ini")
        with open(_cp_off, "w", encoding="utf-8") as _f:
            _f.write("[notices]\nsource = auto\n\n[mail]\nsource = off\nuser = me@qq.com\n"
                     "authcode = SECRET\n")
        check("邮箱配置：[mail] source=off 是明确的关闭出口，有凭据也不连",
              Config(_cp_off, base=_mb).mail_enabled is False)
        _cp_env = os.path.join(_tm, "env.ini")
        with open(_cp_env, "w", encoding="utf-8") as _f:
            _f.write("[mail]\nuser = me@qq.com\nauthcode =\n")
        os.environ["APPLYTRACKER_MAIL_AUTHCODE"] = "ENVSECRET"
        try:
            check("邮箱配置：授权码能走环境变量 APPLYTRACKER_MAIL_AUTHCODE（不必写进明文配置）",
                  Config(_cp_env, base=_mb).mail_authcode == "ENVSECRET")
        finally:
            os.environ.pop("APPLYTRACKER_MAIL_AUTHCODE", None)
        _cp_misc = os.path.join(_tm, "misc.ini")
        with open(_cp_misc, "w", encoding="utf-8") as _f:
            _f.write("[mail]\nsource = imap\nuser = u@q.com\nauthcode = a\n"
                     "only_unseen = yes\ndays_back = 7\nttl = 120\nport = 993\n")
        _cm = Config(_cp_misc, base=_mb)
        check("邮箱配置：only_unseen / days_back / ttl / port 都读得对（增量窗口靠它们）",
              _cm.mail_only_unseen is True and _cm.mail_days_back == 7
              and _cm.mail_ttl == 120 and _cm.mail_port == 993)
        check("邮箱配置：中文文件夹名走 modified UTF-7 编码（IMAP 只吃 ASCII 文件夹名）",
              _mail_utf7_encode("INBOX") == "INBOX"
              and _mail_utf7_encode("收件箱") == "&ZTZO9nux-"
              and _mail_utf7_encode("收件箱").isascii())

        # ---- 规则：五类 + 三处纠偏 ----
        check("邮件识别：五类通知按优先级锁定（资料补充 / AI面 / 笔试 / 测评 / 面试）",
              KNOWN_MAIL_TYPES == ["资料补充", "AI面", "笔试", "测评", "面试"])
        _rules = dict(MAIL_TYPE_RULES)
        check("邮件识别：「视频面试」不再算 AI面（可能是真人面，归到面试）",
              "视频面试" not in _rules["AI面"])
        check("邮件识别：「笔试」从「测评」里拆出来，两边互不含对方的独占词",
              "笔试" in _rules["笔试"] and "笔试" not in _rules["测评"]
              and "测评" not in _rules["笔试"])
        check("邮件识别：新增「资料补充」类（对齐看板的资料补充环节）",
              "资料补充" in _rules and "补充材料" in _rules["资料补充"])

        # ---- 三道闸：强/弱信号、并列降级、留空不猜 ----
        _t, _v, _ = mail_decide("云溪智能2027校园招聘笔试通知", "请按时参加", False)
        check("邮件识别：主题命中「笔试」= 强信号 → 直接定成笔试", _t == "笔试" and _v == "notice", _t)
        _t, _v, _ = mail_decide("XX银行AI面试邀请", "点击链接参加面试", False)
        check("邮件识别：AI面 邮件不会被「面试」抢走（AI面优先，不误并成并列）",
              _t == "AI面" and _v == "notice", _t)
        _t, _v, _ = mail_decide("你好", "请参加我们的测评", False)
        check("邮件识别：正文只命中一个弱信号 → 进待确认区，不标类型（宁可漏过）",
              _t == "" and _v == "review", "%r/%s" % (_t, _v))
        _t, _v, _ = mail_decide("通知", "在线测评 和 性格测试 都要做", False)
        check("邮件识别：正文凑够两个关键词也只是弱信号 → 仍进待确认区",
              _t == "" and _v == "review", "%r/%s" % (_t, _v))
        # 2026-10-08：并列判定原本只比分数，把「顺序即优先级」那一维丢了 —— 加回裸「测评」
        # 之后，「笔试测评通知」两边同为 2 分被判成并列、类型留空。本来认得出的邮件反而
        # 认不出了，这不是用户认的代价，是退化。现在排序键与并列判定共用 _mail_rank。
        _t, _v, _ev = mail_decide("笔试和在线测评都安排好了", "请按时参加", False)
        check("邮件识别：两个类型同分 → 按优先级取靠前的（笔试 > 测评），不再一律降级",
              _t == "笔试" and _v == "notice", "%r/%s" % (_t, _v))
        check("邮件识别：同分靠优先级定出来的，依据里写清楚了（不许静默决胜）",
              set(_ev.get("同分", [])) == {"笔试", "测评"} and "优先级" in _ev, "%r" % (_ev,))
        _t, _v, _ev = mail_decide("笔试测评通知", "", False)
        check("邮件识别：「笔试测评通知」按优先级定成笔试（加裸「测评」之前就是这个行为）",
              _t == "笔试" and _v == "notice", "%r/%s" % (_t, _v))
        # 依据**文字**里也得有 —— 只躺在 evidence 字典里而渲染不出来，等于没写：
        # 用户鼠标停在类型标签上看到的就还是一句干巴巴的「类型「笔试」：主题命中 笔试」。
        check("邮件识别：同分决胜要出现在人看得见的依据文字里，不只是躺在字典里",
              "同分" in mail_evidence_text(_ev) and "优先级" in mail_evidence_text(_ev),
              mail_evidence_text(_ev))
        # ⚠️ 回归钉子：只要候选里有「AI面」就把「面试」**整条从候选里删掉**，
        # 那样一封明确的面试通知（主题写明终面 / 面试，正文只顺口提了句「含 AI面试 环节」）
        # 会从「认得出」被打成「认不出」。2026-10-08 这一版真踩过一次（对比 1a4a2ab 验出来的）。
        check("邮件识别：明确的面试通知不会因正文提了句「AI面试」就被打成认不出",
              mail_decide("终面安排通知", "本次流程含AI面试环节", False)[:2] == ("面试", "notice")
              and mail_decide("面试通知", "请通过AI面试系统完成", False)[:2] == ("面试", "notice"))
        # 用户认了的代价：含「测评」但跟求职无关的邮件也会被标出来（比如「资质测评」）。
        # 这是「放宽、认」的直接结果，不是缺陷 —— 钉住它，免得以后有人当成 bug 反向修掉。
        _t2, _v2, _ = mail_decide("资质测评", "", False)
        check("邮件识别：「资质测评」这类也标成测评（放宽词表有意接受的误召回，别改回去）",
              _t2 == "测评" and _v2 == "notice", "%r/%s" % (_t2, _v2))
        # 2026-10-08 用户要求放宽：裸「测评」算测评（原来只认「在线测评 / 测评邀请」这类组合词，
        # 有的公司邮件就写「测评通知」三个字，不放宽整封就漏了）。
        # 代价用户已经认了 —— 「测评结果反馈」这种已经结束的也会被标出来，所以不许再加否定词去筛。
        check("邮件识别：词表里有裸「测评」（用户明确放宽，别再改回去）",
              "测评" in dict(MAIL_TYPE_RULES)["测评"])
        _t, _v, _ = mail_decide("测评通知", "", False)
        check("邮件识别：主题只写「测评通知」→ 定成测评，且不进待确认区",
              _t == "测评" and _v == "notice", "%r/%s" % (_t, _v))
        check("邮件识别：裸「测评」只放宽了词，没放宽分数 —— 优先级顺序也没动",
              [t for t, _ in MAIL_TYPE_RULES] == KNOWN_MAIL_TYPES
              and mail_decide("测评结果反馈", "", False)[0] == "测评")
        _d0 = (datetime.now() + timedelta(days=20))
        _dl = mail_extract_deadline("请于%d年%d月%d日 23:59前完成" % (_d0.year, _d0.month, _d0.day),
                                    datetime.now(), 8, 0)
        check("邮件识别：写了「请于…前完成」→ 抽得出截止时间",
              _dl is not None and (_dl.year, _dl.month, _dl.day) == (_d0.year, _d0.month, _d0.day),
              str(_dl))
        check("邮件识别：没有日期就留空（默认不按邮件时间硬推一个假截止）",
              mail_extract_deadline("云溪智能笔试通知，请尽快完成", datetime.now(), 8, 0) is None)

        # ---- 公司匹配：只在用户名单里认，绝不臆造 ----
        _ctx = mail_match_ctx(_mc)
        _co, _how, _ = mail_match_company("长河银行杭州分行2027校园招聘笔试通知",
                                          "长河银行杭州分行诚邀你参加笔试",
                                          "长河银行招聘", "hr@changhe-bank.com", _ctx)
        check("公司匹配：「长河银行杭州分行」对得上名单里的「长河银行股份有限公司」",
              _co == "长河银行股份有限公司", "%r/%s" % (_co, _how))
        _co, _how, _ = mail_match_company("星桥科技2027校园招聘笔试通知", "请参加笔试",
                                          "星桥科技招聘", "hr@starbridge.com", _ctx)
        check("公司匹配：名单里没有的公司 → 认不出（空串，不进通知表）",
              _co == "" and _how == "", "%r/%s" % (_co, _how))
        check("公司匹配：域名匹配要求主体 ≥4 字符（免得 abc.com 对到「ABC」乱认）",
              mail_match_company("通知", "内容", "x", "hr@abc.com", _ctx)[0] == "")

        # ---- 端到端：.eml → notices.csv + 旁路文件（不联网） ----
        def _mk_eml(subject, name, addr, body):
            _e = lambda s: "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode() + "?="
            return ("From: %s <%s>\r\nTo: me@qq.com\r\nSubject: %s\r\n"
                    "Date: %s +0800\r\nMessage-ID: <%s@test>\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n"
                    "Content-Transfer-Encoding: base64\r\n\r\n%s"
                    % (_e(name), addr, _e(subject),
                       datetime.now().strftime("%a, %d %b %Y %H:%M:%S"),
                       uuid.uuid4().hex, base64.b64encode(body.encode("utf-8")).decode())
                    ).encode("utf-8")
        _mails = [
            _mk_eml("长河银行杭州分行2027校园招聘笔试通知", "长河银行招聘", "hr@changhe-bank.com",
                    "同学你好，请参加在线笔试；请于%d年%d月%d日 23:59前完成。"
                    % (_d0.year, _d0.month, _d0.day)),
            _mk_eml("星桥科技2027校园招聘面试通知", "星桥科技招聘", "hr@starbridge.com",
                    "请参加面试，具体时间另行通知。"),
            # 公司认得出（长河银行在名单里），但主题正文都没说是什么类型
            # → 类型留空、**照样进通知表**（以前这种是丢进 data/mail_review.csv 那个单独文件）
            _mk_eml("长河银行2027校园招聘公告", "长河银行招聘", "hr@changhe-bank.com",
                    "本次招聘的相关安排请留意后续邮件通知。"),
            _mk_eml("你好", "某公司", "hr@x.com", "请参加我们的测评"),
            _mk_eml("录用结果通知", "某公司", "hr@y.com", "很遗憾，这次未通过。"),
        ]
        _stat = mail_build_notices(_mc, _mails)
        check("邮件同步：类型认得出的进通知表；公司认得出、类型拿不准的**也进通知表**（类型留空）；"
              "公司认不出的进未匹配区（拒信也一样 —— 认不出公司归不了投递记录，但不静默丢）",
              _stat["added"] == 2 and _stat["unknown"] == 1
              and _stat["unmatched"] == 3 and _stat["skipped"] == 0, str(_stat))
        _rows = mail_existing_notice_rows(_mc)
        _typed = [r for r in _rows if r["通知类型"]]
        check("邮件同步：类型认得出的那条用名单里的原名、类型是笔试、抽得出截止时间",
              len(_typed) == 1 and _typed[0]["公司名称"] == "长河银行股份有限公司"
              and _typed[0]["通知类型"] == ["笔试"] and _typed[0]["通知截止时间"] != "",
              str(_typed[:1]))
        # 这条是 Task #20 的核心：类型认不出**不等于**这封邮件没用 —— 公司认得出就该出现在列表里，
        # 只是类型为空。「待确认」三个字是前端渲染角标时加的 UI 文案，数据里必须是空串。
        check("邮件同步：公司认得出、类型拿不准的那条照样在通知表里，类型是空串（不是「待确认」字样）",
              len(_rows) == 2 and any(r["公司名称"] == "长河银行股份有限公司"
                                      and r["通知类型"] == [] for r in _rows), str(_rows))
        check("邮件同步：认不出公司的那三封**不**进通知表（不知道是哪家的，列出来是没主儿的一行）",
              sorted((r.get("邮件主题") or "").strip() for r in _rows) ==
              sorted(["长河银行杭州分行2027校园招聘笔试通知", "长河银行2027校园招聘公告"]),
              str(sorted((r.get("邮件主题") or "") for r in _rows)))
        # 公司认不出 ↔ 类型拿不准 得落在两个不同的出口，不能混成一个桶
        check("邮件同步：公司认不出走 mail_unmatched，类型拿不准走通知表 —— 两个出口不串",
              sorted(r.get("发件人域名") for r in mail_unmatched_rows(_mc)) ==
              ["starbridge.com", "x.com", "y.com"])
        check("邮件同步：识别依据写进旁路 notice_evidence.json（键 = notice_state_key）",
              len(mail_load_evidence(_mc)) == 2)
        _nts = load_notices(_mc)
        _nt_typed = [n for n in _nts if n["通知类型"]]
        check("邮件同步：依据能挂回通知行（「为什么标成这个类型」说得清，可解释）",
              len(_nt_typed) == 1 and mail_attach_evidence(_mc, _nts) == 2
              and "笔试" in _nt_typed[0]["依据"] and "长河银行" in _nt_typed[0]["依据"],
              str(_nt_typed[0].get("依据") if _nt_typed else None))
        check("邮件同步：类型留空那条的依据写的是「拿不准」的理由，不是假的类型",
              any((n.get("依据") or "").find("没命中任何类型词") >= 0
                  for n in _nts if not n["通知类型"]),
              str([n.get("依据") for n in _nts if not n["通知类型"]]))
        check("邮件同步：未匹配区留下了发件人域名（页面上点一下归类要用）",
              any(r.get("发件人域名") == "starbridge.com" for r in mail_unmatched_rows(_mc)))
        check("邮件同步：再跑一遍不重复进通知表（靠 .mail_state + 内容键去重）",
              mail_build_notices(_mc, _mails)["added"] == 0)
        check("邮件同步：归过类之后同一发件人自动对上（越用越准，不用再点一次）",
              set_mail_alias(_mc, "starbridge.com", "星桥科技")
              and mail_match_company("星桥科技2027校园招聘面试通知", "请参加面试",
                                     "星桥科技招聘", "hr@starbridge.com",
                                     mail_match_ctx(_mc))[:2] == ("星桥科技", "别名"))
        check("邮件同步：通知表列没变（还是 NOTICE_FIELDS + 发件人，证据走旁路）",
              set(NOTICE_FIELDS) <= set(_rows[0].keys()) and "为什么" not in _rows[0])

        # ---- 2026-10-08 识别规则对齐飞书链路（规则源：作者的邮箱→飞书自动化提示词）----
        _ctxm = mail_match_ctx(_mc)
        _p = mail_parse_one(_mk_eml("投递成功确认", "长河银行招聘", "hr@changhe-bank.com",
                                    "已收到你的投递，我们处理后会尽快通知你。"), _mc, _ctxm)
        check("邮件识别：投递成功回执被拒（新负面词，别让它冒充通知）",
              _p["_verdict"] == "skip", "%r" % (_p.get("_verdict"),))
        # 「感谢」单字绝不拒 —— offer 邮件也满嘴感谢；必须「感谢 + 坏消息词」成对同现才算拒信。
        # 2026-10-08 她追加：拒信 / Offer 不再静默丢 —— offer 邮件认成 Offer 类型（两路的入口）。
        _p = mail_parse_one(_mk_eml("长河银行2027校园招聘录用通知", "长河银行招聘", "hr@changhe-bank.com",
                                    "感谢您参加本次招聘。我们很高兴地通知您：您已被录用，请于3日内回复确认。"),
                            _mc, _ctxm)
        check("邮件识别：offer 邮件带「感谢」不被误杀 —— 认成 Offer 类型（不落待确认、更不丢）",
              _p["_verdict"] == "offer" and _p.get("通知类型") == ["Offer"],
              "%r/%r" % (_p.get("_verdict"), _p.get("通知类型")))
        _p = mail_parse_one(_mk_eml("长河银行2027校园招聘结果通知", "长河银行招聘", "hr@changhe-bank.com",
                                    "感谢您的关注，很遗憾这一次未能与你同行。"), _mc, _ctxm)
        check("邮件识别：「感谢 + 遗憾」成对同现 → 判拒信（类型标「拒信」，不静默丢）",
              _p["_verdict"] == "reject" and _p.get("通知类型") == ["拒信"] and
              any("遗憾" in h for h in (_p.get("_evidence") or {}).get("命中词", [])),
              "%r/%r" % (_p.get("_verdict"), _p.get("_evidence")))
        # ---- 2026-10-09 录用判定大修（她报的红框：4 行假 Offer）----
        # 夹具就是那 6 封真实邮件的主题，逐字抄回来的（公司名已脱敏）。当时全都栽在
        # 「正文里有录用词 / 公司名裸子串」这两条上。
        for _subj, _why in [
            ("【XX校园招聘】在线人才测评邀请", "测评邀请被正文一个「录用」翻成 Offer"),
            ("Release Date Announcement: Megastructures Expansion for GalCiv IV", "2024 年的游戏广告"),
            ("little, top price drops in Hurghada, selected for you!", "Agoda 酒店促销"),
            ("简历已收到，谢谢你选择 XX 有限公司｜正文可查看后续招聘流程", "投递回执"),
            ("XX科技2027校园招聘简历接收函", "简历接收函"),
            ("【京东校招】多彩的京东欢迎你", "入职通知但主题没写录用词 —— 宁可漏给她看"),
        ]:
            check("录用判定：主题不像录用文书的一律不算 Offer（%s）" % _why,
                  mail_offer_hit(_subj) == ([], False), str(mail_offer_hit(_subj)))
        check("录用判定：真录用文书的主题照常认出，且分得出强弱（强词可直接推状态）",
              mail_offer_hit("长河银行2027校园招聘录用通知") == (["录用通知"], True)
              and mail_offer_hit("恭喜你被录取") == (["录取"], False),
              "%s / %s" % (mail_offer_hit("长河银行2027校园招聘录用通知"),
                           mail_offer_hit("恭喜你被录取")))
        check("录用判定：英文录用词按整词匹配（offers 不算，offer letter 算）",
              mail_offer_hit("Great offers inside") == ([], False)
              and mail_offer_hit("Your offer letter is ready")[1] is True,
              "%s / %s" % (mail_offer_hit("Great offers inside"),
                           mail_offer_hit("Your offer letter is ready")))
        # 正文里的录用词一律不算 —— 这就是这次误判的根因
        set_mail_alias(_mc, "u-ctx.com", "示例语境公司")
        _ctxc = mail_match_ctx(_mc)
        _po = mail_parse_one(_mk_eml("【示例语境公司校园招聘】在线人才测评邀请", "示例语境公司招聘",
                                     "hr@u-ctx.com",
                                     "请完成在线测评，测评结果将进入录用流程。"),
                             _mc, _ctxc)
        check("录用判定：正文里出现「录用」不再把测评邀请翻成 Offer（主题说了算）",
              _po.get("_verdict") == "notice" and _po.get("通知类型") == ["测评"],
              "%r/%r" % (_po.get("_verdict"), _po.get("通知类型")))
        _po2 = mail_parse_one(_mk_eml("简历已收到，谢谢你选择示例语境公司", "示例语境公司招聘",
                                      "hr@u-ctx.com",
                                      "如后续合适，我们会为你 offer 一个岗位。"),
                              _mc, _ctxc)
        check("录用判定：正文里的英文 offer 不再把投递回执翻成 Offer",
              _po2.get("_verdict") != "offer" and (_po2.get("通知类型") or []) != ["Offer"],
              "%r/%r" % (_po2.get("_verdict"), _po2.get("通知类型")))
        # 公司名匹配：不再"删掉空白后裸数子串"
        _hitv = mail_name_hit(
            "Release Date Announcement: Megastructures Expansion for GalCiv IV",
            "Stardock Entertainment",
            "A versatile megastructure, research hub or trading post. GalCiv IV Overview",
            ["vivo", "Sea", "美团"], "communications@stardockentertainment.info")
        check("公司名匹配：GalCiv 那封游戏广告不再命中 vivo / Sea（去空格拼串的毛病）",
              _hitv.get("hit") == "" and _hitv.get("sure") is False, str(_hitv))
        _hitv2 = mail_name_hit("【vivo校园招聘】2027在线测评邀请", "vivo招聘", "",
                               ["vivo", "美团"])
        check("公司名匹配：发件人里真出现整词 vivo 照常认得出（收紧不等于认不出真实公司）",
              _hitv2.get("hit") == "vivo", str(_hitv2))
        check("公司名匹配：纯英文名只在正文里出现不算数（正文噪音太大）",
              mail_name_hit("校园招聘通知", "某招聘", "欢迎加入 vivo 生态", ["vivo"],
                            "hr@example.com").get("hit") == "")
        check("公司名匹配：酒店信里单独的 sea 不该认成公司 Sea（3 字母英文词不在发件人里就不认）",
              mail_name_hit("Sea view rooms on sale", "Agoda", "book now", ["Sea"],
                            "promo@agoda.com").get("hit") == "")
        check("公司名匹配：「京东」不再等于「京东方」（子串并要求短名 ≥3 字）",
              mail_same_company("京东", "京东方") is False
              and mail_same_company("京东集团", "京东方") is False
              and mail_same_company("长河银行杭州分行", "长河银行") is True
              and mail_same_company("星桥科技", "星桥科技") is True)
        check("邮箱扫描窗口：搜索条件分开传，绝不裹括号（裹了 QQ 会忽略条件、返回全库）",
              mail_since_criteria(7, False, datetime(2026, 10, 9, 12, 0)) == ["SINCE", "02-Oct-2026"]
              and mail_since_criteria(7, True, datetime(2026, 10, 9, 12, 0)) ==
              ["UNSEEN", "SINCE", "02-Oct-2026"]
              and not any(str(x).startswith("(") for x in
                          mail_since_criteria(30, False, datetime(2026, 10, 9, 12, 0))),
              str(mail_since_criteria(7, False, datetime(2026, 10, 9, 12, 0))))
        set_mail_alias(_mc, "u-real.com", "示例公司")
        _p = mail_parse_one(_mk_eml("请参加示例公司2027UFLP管理培训生项目在线测评", "示例公司招聘",
                                    "hr@u-real.com", "请点击链接完成在线测评。"), _mc,
                            mail_match_ctx(_mc))
        # 公司认得出（别名命中，同下面 u-ctx 那组的套路）→ verdict=notice 是对的。
        # 这条钉的是「没被新负面词误伤」：类型照常认出测评、照常进表。
        check("邮件识别：正面用例不被新负面词误伤 —— 真实标题的测评邀请照常认出类型、照常进通知表",
              _p["_verdict"] == "notice" and _p.get("通知类型") == ["测评"],
              "%r/%r" % (_p.get("_verdict"), _p.get("通知类型")))
        _mt = datetime(2026, 10, 8, 10, 0, 0)
        _rd = mail_extract_deadline("请于收到本通知起的72小时内完成在线测评", _mt, 8, 0)
        check("邮件识别：「收到本通知起的72小时」→ 收到时间 + 72 小时（相对口径，不带「内」也算）",
              _rd is not None and _rd == _mt + timedelta(hours=72), str(_rd))
        check("邮件识别：催办词表就位（提醒/催办/再次/未完成/尽快完成/截止在即）",
              MAIL_BUMP_HINTS == ["提醒", "催办", "再次", "未完成", "尽快完成", "截止在即"])
        # 催办去重（端到端）：笔试通知已在表里（上面 e2e 加进来的），
        # 同公司同类型**且截止时间相同**的催办邮件 → skip、不另立一行、证据落 skip 日志。
        # 2026-10-08 她追加：去重要按截止时间收紧 —— 同截止（或双方都无）才算同一件事的催。
        _stat2 = mail_build_notices(_mc, [
            _mk_eml("【再次提醒】长河银行笔试还未完成，截止在即", "长河银行招聘", "hr@changhe-bank.com",
                    "你的笔试还未完成，请尽快完成。请于%d年%d月%d日 23:59前完成。"
                    % (_d0.year, _d0.month, _d0.day))])
        check("邮件同步：同公司同类型同截止的催办被去重 —— 不另立一行（added=0），进跳过区（skipped=1）",
              _stat2["added"] == 0 and _stat2["skipped"] == 1, str(_stat2))
        with open(mail_skipped_path(_mc), encoding="utf-8") as _f:
            _skip_rows = [json.loads(_l) for _l in _f if _l.strip()]
        check("邮件同步：催办的证据落进 skip 日志，写明「对已有记录的重复催办」",
              any("重复催办" in (o.get("为什么") or "") for o in _skip_rows),
              str(_skip_rows[-1:]))
        # 截止时间不同 = 可能是改期 / 重新安排的测评 —— 新截止时间必须进面板，照常记录
        _stat2b = mail_build_notices(_mc, [
            _mk_eml("【再次提醒】长河银行笔试还未完成，截止在即", "长河银行招聘", "hr@changhe-bank.com",
                    "笔试时间已调整，请于%d年%d月%d日 23:59前完成。"
                    % (_d0.year, _d0.month, _d0.day + 1))])
        check("邮件同步：同公司同类型但截止时间不同的催办**照常进表**（改期不是催，别跳过）",
              _stat2b["added"] == 1 and _stat2b["skipped"] == 0, str(_stat2b))
        check("邮件直连：按域名推断服务器 —— 各家归各位，认不出返回空（不瞎猜）",
              mail_infer_server("a@qq.com") == ("imap.qq.com", 993)
              and mail_infer_server("a@foxmail.com")[0] == "imap.qq.com"
              and mail_infer_server("a@163.com") == ("imap.163.com", 993)
              and mail_infer_server("a@126.com") == ("imap.126.com", 993)
              and mail_infer_server("a@gmail.com") == ("imap.gmail.com", 993)
              and mail_infer_server("a@outlook.com")[0] == "outlook.office365.com"
              and mail_infer_server("a@school.edu.cn") == ("", 0))
        # /api/mail-setup 的静态守卫：端点在 POST 路由上；写盘之后的所有响应段
        # （内存 cfg 接线 + 成功返回 + 起同步）不含 authcode —— 密码级凭据绝不回显
        _ms = src.split('"/api/mail-setup"', 1)[1].split('"/api/mail-sync"', 1)[0]
        _ms_tail = _ms.split("# 内存里的 cfg 同步改掉", 1)[1]
        check("邮件直连：/api/mail-setup 挂在 POST 上，写盘之后 authcode 全程不出（响应零回显）",
              _ms.count('"authcode"') == 2 and '"authcode"' not in _ms_tail
              and '"user": email,' in _ms)
        check("邮件直连：连接弹卡是原生控件（label/details/原生 button），Esc 只挂卡内输入框不挂全局",
              'id="mailSetupWrap"' in html and '<button id="mstGo" class="primary">连接</button>' in html
              and "<details" in html and 'for="mstEmail"' in html
              and "openMailSetup" in html
              and 'addEventListener("keydown"' not in html)
        # ⚠️ 弹卡常显的坑（2026-10-08 她的原话「我输完连接完邮箱后之后这个还在。。」）：
        # #mailSetupWrap 的 CSS 里写了 display:flex（作者样式），**优先级高于 UA 的
        # [hidden]{display:none}** → closeMailSetup() 的 hidden=true 形同虚设，弹卡带
        # 全屏遮罩（position:fixed + z-index:60）常显，把整页都盖住。作者 display 与
        # [hidden] 补丁必须成对出现 —— 本文件里 .form[hidden] / #appOpenAll[hidden] 就是范本。
        # 这条断言只钉字符串；「真的看不见」由 board-check 用 getComputedStyle 实测（读 el.hidden 会假绿）。
        check("邮件直连：弹卡的 [hidden] 补丁在（作者 display 会压过 UA 的 [hidden]，两者必须成对）",
              'id="mailSetupWrap"' in html and "#mailSetupWrap[hidden]{display:none}" in html)

        # ---- v0.33.4：连完邮箱必须看得见 + 数据源由用户自己点 ----
        # 她的原话「我输完连接完邮箱后之后这个还在。。」（2026-10-08）：
        # /api/mail-setup 把 [mail] 写了、回了 ok，但她的 [notices] source=feishu 没动 →
        # 面板一个字节都没变，前端还撒谎说「正在收第一轮邮件」；而 feishu 模式下 mailBar()
        # 直接 el.hidden=true，连完邮箱整条藏起来，她当然「看不见」。
        # 修法：① 响应带真相（mode / switched）② 新增两个把手让她自己切源 ③ 状态常驻可见。
        # ⚠️ 端点名在**前端 JS 里也出现**（fetch("/api/mail-source")），必须只在 do_POST 那一段里切，
        # 否则 split 会切在页脚脚本上（踩过）。
        _post = src[src.index("def do_POST"):]
        check("邮箱连接状态：/api/mail-setup 写 [mail] 是整套键（folder/days_back/only_unseen/ttl 一起写，不留半套）",
              all(('"%s"' % _k) in _ms for _k in ("folder", "days_back", "only_unseen", "ttl"))
              and '"folder": c.mail_folder or "INBOX"' in _ms)
        check("邮箱连接状态：/api/mail-setup 响应带 mode 与 switched（页面才说得清「连上≠已启用」）",
              '"mode": c.notices_source' in _ms and '"switched": switched' in _ms)
        _msrc = _post.split('"/api/mail-source"', 1)[1].split('"/api/mail-off"', 1)[0]
        _moff = _post.split('"/api/mail-off"', 1)[1][:900]
        check("邮箱连接状态：/api/mail-source 挂在 POST 上，响应段零回显 authcode，切完起一轮后台同步",
              '"/api/mail-source"' in _post and "sync_notices" in _msrc
              and "authcode" not in _msrc
              and '{"ok": True, "mode": want' in _msrc)
        check("邮箱连接状态：/api/mail-off 挂在 POST 上，断开只写 [mail] source=off（凭据留着）",
              '"/api/mail-off"' in _post
              and '("mail", "source", "off")' in _post
              and "authcode" not in _moff
              and '"mode": c.notices_source' in _moff)
        check("邮箱连接状态：切 imap 前要凭据、切 feishu 前要飞书表（不然 400 说不清）",
              "还没连过邮箱" in _msrc and "c.feishu_base and c.feishu_table" in _msrc)
        # 「断开」之后的一键回连：只写 [mail] source=auto，**不动 [notices] source** ——
        # 面板读哪个源由用户按「改用直连收信 / 切回飞书表」决定，不允许偷偷换源。
        _mon = _post.split('"/api/mail-on"', 1)[1][:700]
        check("邮箱连接状态：/api/mail-on 一键回连只写 [mail] source=auto，不碰 [notices]（不偷偷换源）",
              '"/api/mail-on"' in _post and '("mail", "source", "auto")' in _post
              and '("notices"' not in _mon and "authcode" not in _mon)
        check("邮箱连接状态：断开之后页面给『重新连上』而不是又要一遍授权码（能不给她的地方别让她手打）",
              'id="mailOnBtn"' in html and "META.mail_configured" in html
              and "mailOnBtn" in html.split("function mailBar()", 1)[1][:2400]
              and "restore" not in html.split('id="mailOnBtn"', 1)[1][:200])
        check("邮箱连接状态：状态行里写的是**哪个邮箱**（只写「邮箱」两个字等于没说）",
              "已连上 <b>' + who + '" in html)
        # 真跑一遍 /api/mail-off 的写盘（纯函数那一层）：一次 ini_patch 写两段，
        # Config 读回来要是「邮箱关了 + 源落回飞书 + 授权码还在」—— 断开不等于删凭据。
        _cp_offw = os.path.join(_tm, "offw.ini")
        with open(_cp_offw, "w", encoding="utf-8") as _f:
            _f.write("[notices]\nsource = imap\nfeishu_base_token = B\nfeishu_table_id = T\n\n"
                     "[mail]\nsource = auto\nhost = imap.qq.com\nport = 993\nuser = me@qq.com\n"
                     "authcode = SECRET\n")
        ini_patch(_cp_offw, [("mail", "source", "off"), ("notices", "source", "feishu")])
        _coff = Config(_cp_offw, base=_mb)
        check("邮箱连接状态：/api/mail-off 一次写两段 —— 邮箱关掉、源落回飞书、授权码还留着（下次点回来不用重填）",
              _coff.mail_enabled is False and _coff.notices_source == "feishu"
              and _coff.mail_authcode == "SECRET" and _coff.mail_configured is True)
        # /api/mail-source 的写盘：把 [notices] source 切到 imap，Config 读回来就得是 imap
        _cp_sw = os.path.join(_tm, "sw.ini")
        with open(_cp_sw, "w", encoding="utf-8") as _f:
            _f.write("[notices]\nsource = feishu\nfeishu_base_token = B\nfeishu_table_id = T\n\n"
                     "[mail]\nsource = auto\nuser = me@qq.com\nauthcode = SECRET\n")
        ini_patch(_cp_sw, [("notices", "source", "imap")])
        check("邮箱连接状态：/api/mail-source 切到 imap 后 Config 读回来就是邮箱直连",
              Config(_cp_sw, base=_mb).notices_source == "imap")
        check("邮箱连接状态：mailBar() 在非 imap 模式下也渲染邮箱状态（这次 bug 的正面修复）",
              "mail_configured && META.mail_enabled" in html and "mailStatusHTML" in html
              and "备用直连，没在用" in html and "mailUseBtn" in html
              and 'id="mailOffBtn"' in html)
        check("邮箱连接状态：连接成功不再一律说「正在收第一轮邮件」—— 按 switched 分两种说法",
              "switched === true" in html and "正在收第一轮邮件" in html
              and "要改成直连就点「改用直连收信」" in html)
        check("邮箱连接状态：没凭据时任何模式都给「连接我的邮箱」入口（不再只在 off 模式出现）",
              html.count('id="mailSetupBtn"') >= 2 and "连接我的邮箱" in html)

        # ---- 拒信 / Offer 两路（2026-10-08 她追加：拒信要反映到投递进度条里，别静默丢）----
        # 路一：匹配到投递记录 → 拒信整家标止步（止步来源=邮件）、Offer 状态推上去（手改不覆盖）；
        # 路二：匹配不到 → 进通知表标「拒信」/「Offer」。CSV 列一列不动（ADR-005），依据走 JSON。
        add_app(_mc, "星桥科技", position="测试开发", status="测评笔试")
        _statr = mail_build_notices(_mc, [
            _mk_eml("星桥科技2027校园招聘结果通知", "星桥科技招聘", "hr@starbridge.com",
                    "感谢您的关注，很遗憾这一次未能与你同行。")])
        check("拒信两路（路一）：匹配到投递记录的拒信 → 落投递进度、不进通知表（applied=1）",
              _statr["applied"] == 1 and _statr["added"] == 0, str(_statr))
        _ivr = load_interview_progress(_mc).get(interview_key("星桥科技")) or {}
        check("拒信两路（路一）：止步来源记「邮件」、依据留了邮件主题（进度行上看得见凭据）",
              _ivr.get("止步") is True and _ivr.get("止步来源") == "邮件"
              and "星桥科技2027校园招聘结果通知" in (_ivr.get("邮件依据") or {}).get("主题", ""),
              str(_ivr))
        # 多岗别一刀切：她钉过「一家多岗只挂了一岗，不整家算止步」—— 还有活口就只挂依据
        add_app(_mc, "长河银行", position="客户经理", status="简历通过")
        add_app(_mc, "长河银行", position="风控专员", status="已投递")
        _statr2 = mail_build_notices(_mc, [
            _mk_eml("长河银行杭州分行2027校园招聘结果通知", "长河银行招聘", "hr@changhe-bank.com",
                    "感谢您的关注，很遗憾未能通过本轮筛选。")])
        _ivr2 = load_interview_progress(_mc).get(interview_key("长河银行")) or {}
        check("拒信两路：一家多岗还有岗在跑 → 不整家算止步（依据照挂，止步留给她自己点）",
              _statr2["applied"] == 1 and _ivr2.get("止步") is None
              and bool((_ivr2.get("邮件依据") or {}).get("主题")), str(_ivr2))
        # Offer 路一：匹配到、没手改过（还是默认已投递）→ 状态自动推到「Offer」
        set_mail_alias(_mc, "u-new.com", "示例新公司")
        add_app(_mc, "示例新公司", position="产品培训生", status="已投递")
        _stato = mail_build_notices(_mc, [
            _mk_eml("示例新公司2027校园招聘录用通知", "示例新公司招聘", "hr@u-new.com",
                    "感谢您参加本次招聘。我们很高兴地通知您：您已被录用，请于3日内回复确认。")])
        _appo = [a for a in load_apps(_mc) if a["岗位名称"] == "产品培训生"][0]
        check("Offer 两路（路一）：匹配到投递记录、她没手改过 → 状态自动推到「Offer」",
              _stato["applied"] == 1 and _appo["状态"] == "Offer",
              "%s/%s" % (_stato, _appo.get("状态")))
        # Offer 路一（铁律）：她手改过的状态绝不被邮件覆盖 —— 本地行：动了默认态 = 手改
        add_app(_mc, "示例新公司", position="运营专员", status="面试")
        _stato2 = mail_build_notices(_mc, [
            _mk_eml("示例新公司2027校园招聘录用通知（运营专员）", "示例新公司招聘", "hr@u-new.com",
                    "恭喜！您已被录用，请尽快确认。")])
        _appo2 = [a for a in load_apps(_mc) if a["岗位名称"] == "运营专员"][0]
        check("Offer 两路（铁律）：她手改过的状态（本地行已动过默认态）绝不被邮件覆盖",
              _appo2["状态"] == "面试", "%s/%s" % (_stato2, _appo2.get("状态")))
        # Offer 路一（铁律·飞书口径）：状态偏离「飞书状态」基线 = 手改过，同 merge_apps 的冲突规则
        _appf = add_app(_mc, "示例新公司", position="飞书岗", status="已投递", source="飞书表")[0]
        update_app(_mc, _appf["id"], 飞书状态="已投递")
        update_app(_mc, _appf["id"], 状态="面试")
        _stato3 = mail_build_notices(_mc, [
            _mk_eml("示例新公司2027校园招聘录用通知（飞书岗）", "示例新公司招聘", "hr@u-new.com",
                    "恭喜！您已被录用。")])
        _appo3 = [a for a in load_apps(_mc) if a["岗位名称"] == "飞书岗"][0]
        check("Offer 两路（铁律·飞书口径）：状态偏离飞书状态基线 = 她手改过，邮件不覆盖",
              _appo3["状态"] == "面试", "%s/%s" % (_stato3, _appo3.get("状态")))
        # 路二：匹配不到任何投递记录 → 进通知表标「拒信」/「Offer」，可见、不静默丢
        set_mail_alias(_mc, "u-pass2.com", "示例无记录公司")
        _statr3 = mail_build_notices(_mc, [
            _mk_eml("示例无记录公司2027校园招聘结果通知", "示例无记录公司招聘", "hr@u-pass2.com",
                    "感谢您的关注，很遗憾这一次未能与你同行。"),
            _mk_eml("示例无记录公司2027校园招聘录用通知", "示例无记录公司招聘", "hr@u-pass2.com",
                    "恭喜您！您已通过全部环节，正式录用。")])
        _rowsr3 = mail_existing_notice_rows(_mc)
        check("拒信两路（路二）：匹配不到投递记录的拒信 → 进通知表标「拒信」（不静默丢）",
              _statr3["added"] == 2 and
              any("示例无记录公司" in r["公司名称"] and r["通知类型"] == ["拒信"] for r in _rowsr3),
              "%s" % [r["通知类型"] for r in _rowsr3 if "示例无记录公司" in r["公司名称"]])
        check("Offer 两路（路二）：匹配不到投递记录的 Offer → 进通知表标「Offer」（不静默丢）",
              any("示例无记录公司" in r["公司名称"] and r["通知类型"] == ["Offer"] for r in _rowsr3),
              "%s" % [r["通知类型"] for r in _rowsr3 if "示例无记录公司" in r["公司名称"]])
        # ---- 旁证闸（2026-10-09 她的原话：真录用了，前面几封面试通知不可能一封都没有）----
        # 甲：主题只有弱词、这家一条测评/面试通知都没有 → 依据照挂，但**不推状态**
        set_mail_alias(_mc, "u-weak-a.com", "示例旁证甲")
        add_app(_mc, "示例旁证甲", position="运营", status="已投递")
        _stwa = mail_build_notices(_mc, [
            _mk_eml("示例旁证甲2027校园招聘录取通知", "示例旁证甲招聘", "hr@u-weak-a.com",
                    "恭喜你，你已被录取。")])
        _appa = [a for a in load_apps(_mc) if a["公司名称"] == "示例旁证甲"][0]
        _iva = load_interview_progress(_mc).get(interview_key("示例旁证甲")) or {}
        check("录用旁证闸：只有弱词、这家又一条测评/面试通知都没有 → 不推状态（依据照挂给人看）",
              _stwa["applied"] == 1 and _appa["状态"] == "已投递"
              and bool((_iva.get("邮件依据") or {}).get("主题")),
              "%s/%s" % (_appa.get("状态"), _iva))
        # 乙：这家先有过测评通知 → 同一封弱词 Offer 就能推上去
        set_mail_alias(_mc, "u-weak-b.com", "示例旁证乙")
        add_app(_mc, "示例旁证乙", position="运营", status="已投递")
        mail_build_notices(_mc, [
            _mk_eml("示例旁证乙2027校园招聘在线测评邀请", "示例旁证乙招聘", "hr@u-weak-b.com",
                    "请完成在线测评。")])
        _stwb = mail_build_notices(_mc, [
            _mk_eml("示例旁证乙2027校园招聘录取通知", "示例旁证乙招聘", "hr@u-weak-b.com",
                    "恭喜你，你已被录取。")])
        _appb = [a for a in load_apps(_mc) if a["公司名称"] == "示例旁证乙"][0]
        check("录用旁证闸：这家先有过测评通知 → 弱词 Offer 照常把状态推上去",
              _stwb["applied"] == 1 and _appb["状态"] == "Offer",
              "%s/%s" % (_stwb, _appb.get("状态")))
        # 强词不看旁证：录用通知书本身就是凭证
        set_mail_alias(_mc, "u-strong.com", "示例强词丙")
        add_app(_mc, "示例强词丙", position="运营", status="已投递")
        _stwc = mail_build_notices(_mc, [
            _mk_eml("示例强词丙2027校园招聘录用通知", "示例强词丙招聘", "hr@u-strong.com",
                    "现正式录用，请于3日内回复确认。")])
        _appc = [a for a in load_apps(_mc) if a["公司名称"] == "示例强词丙"][0]
        check("录用旁证闸：强词主题（录用通知）不需要旁证，直接推状态",
              _stwc["applied"] == 1 and _appc["状态"] == "Offer",
              "%s/%s" % (_stwc, _appc.get("状态")))
        check("拒信两路：CSV 列一列没动（ADR-005）—— 结果落 interview_progress.json，不落在表结构上",
              set(CSV_FIELDS) == {"id", "公司名称", "岗位名称", "状态", "飞书状态", "投递时间",
                                  "网页链接", "来源", "备注", "更新时间"})
        check("拒信两路：前端进度行挂得出依据（邮件依据 + ivev 渲染 + 邮件来源的已挂提示）",
              '"邮件依据"' in html and 'class="ivev"' in html
              and 'it["止步来源"] === "邮件"' in html)

        # ---- 页面上挑类型：写回 notices.csv，并把挂在旧 key 上的东西搬过去 ----
        # key 是「公司+类型+截止+主题」算的哈希，**改类型 key 就变了**；不搬家的话
        # 用户刚标完「已处理」，改一下类型那标记就悄悄没了 —— 这是最容易静默丢数据的一处。
        _rows0 = mail_existing_notice_rows(_mc)
        _untyped = [r for r in _rows0 if not r["通知类型"]][0]
        _k_old = notice_state_key(_untyped)
        _stp = notice_states_path(_mc)
        with open(_stp, "w", encoding="utf-8") as _f:
            json.dump({_k_old: {"状态": "已完成", "时间": "2026-10-08 10:00"}}, _f, ensure_ascii=False)
        check("通知类型：能手填一条「待确认」的类型（写回 notices.csv，不留旁路覆盖）",
              set_notice_type(_mc, _k_old, "面试") is True)
        _k_new = notice_state_key([r for r in mail_existing_notice_rows(_mc)
                                   if (r.get("邮件主题") or "").strip()
                                   == (_untyped.get("邮件主题") or "").strip()
                                   and r["通知类型"]][0])
        check("通知类型：填完这条在表里就是那个类型了（不是「待确认」三个字）",
              any(r["通知类型"] == ["面试"] for r in mail_existing_notice_rows(_mc)))
        check("通知类型：原先标的处理状态跟着搬到新 key（不然手标就白标了）",
              _k_new != _k_old and bool((_json_load(_stp, {}).get(_k_new) or {}).get("状态") == "已完成"))
        check("通知类型：再改成空 = 回到待确认（能撤回，不是一次性）",
              set_notice_type(_mc, _k_new, "") is True
              and notice_state_key([r for r in mail_existing_notice_rows(_mc)
                                    if (r.get("邮件主题") or "").strip()
                                    == (_untyped.get("邮件主题") or "").strip()
                                    and not r["通知类型"]][0]) == _k_old)
        check("通知类型：乱写的类型不收（免得以后出现第六类找不着北）",
              set_notice_type(_mc, _k_old, "随便写") is False)

        # 「四种结论各有一条」这条必须端到端跑一遍 —— v0.31.0 真踩过：改了结论名
        # （review → unknown）却漏了 --mail-eml 里那个「结论 → 中文名」的字典，
        # 那条命令直接 KeyError 崩掉，而单元测试只看封装好的函数，是查不出来的。
        _eml_dir = os.path.join(_tm, "emls")
        os.makedirs(_eml_dir, exist_ok=True)
        for _i, _raw in enumerate(_mails):
            with open(os.path.join(_eml_dir, "m%d.eml" % _i), "wb") as _f:
                _f.write(_raw)
        try:
            import contextlib as _cl
            import io as _io
            _buf = _io.StringIO()
            with _cl.redirect_stdout(_buf):
                _rep = mail_eml_report(_mc, _eml_dir)
            _rep_out = _buf.getvalue()
        except Exception as _e:
            _rep, _rep_out = -1, "%r" % (_e,)
        check("离线自查 --mail-eml：四种结论都打得出来，加了新结论也不会让这条命令崩",
              _rep == 0 and "待确认（照样进列表）" in _rep_out
              and "没认出公司" in _rep_out and "跳过" in _rep_out, _rep_out[-300:])

        # ---- 配置向导：就地改 ini，保留注释 ----
        _ini = "[mail]\n; 注释要留着\nsource = off\nhost = imap.qq.com\n\n[notices]\nsource = auto\n"
        _ini2 = _ini_set_text(_ini, "mail", "source", "imap")
        check("配置向导：就地改 ini 的值，别的注释与段落都留着（不整份重写）",
              "; 注释要留着" in _ini2 and "source = imap" in _ini2
              and _ini2.count("[notices]") == 1)
        _ini3 = _ini_set_text(_ini, "mail", "user", "me@qq.com")
        check("配置向导：段里没有的键补在段末尾（不插错段）",
              _ini3.index("user = me@qq.com") < _ini3.index("[notices]"))
        check("配置向导：测试登录函数存在且是真连（离线时不可能返回成功）",
              mail_test_login("127.0.0.1", 1, "x", "y")[0] is False)

        # ---- 页面：提示条挂载点 + 脚本 + 五类配色 ----
        _html = render_html(_mc)
        check("邮箱页有提示条的挂载点和渲染脚本（没配给「怎么配」、没认出公司给待办）",
              'id="ntMailBar"' in _html and "function mailBar()" in _html
              and "mailBar();" in _html and "function mailAlias(" in _html)
        check("提示条只在「一个来源都没接」时给怎么配（已在用飞书 / 已有 CSV 的人不被打扰）",
              'META.notices_mode === "off"' in _html)
        check("通知类型配色/顺序覆盖新的五类",
              'const TYPE_ORDER = ["资料补充","AI面","笔试","测评","面试"' in _html
              and '"资料补充":"var(--faint)"' in _html)

        # ---- 端口兜底：区间常量对账 + 真占端口验一次（不真验就等于没写）----
        # 区间写了两份（这里 + start.command 的 BASE/LAST），最容易悄悄漂，拿源码逐字对一遍
        _scp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "start.command")
        if os.path.exists(_scp):
            with open(_scp, encoding="utf-8") as _f:
                _sct = _f.read()
            check("端口区间跟 start.command 的 BASE/LAST 逐字对得上（改一处必须两处都改）",
                  ("BASE=%d" % PORT_BASE) in _sct and ("LAST=%d" % PORT_LAST) in _sct)
        _pip = src.find("def pick_port(")
        _pps = src[_pip:src.find("def serve(", _pip)] if _pip >= 0 else ""
        check("端口兜底：pick_port 存在，且起不来时是抛错（不静默返回）",
              bool(_pps) and "raise OSError(" in _pps and "ThreadingServer((host, p)" in _pps)
        # 真占一个端口，验两件事：① 自己往上让 ② 一个都不剩时抛错
        _blk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _fall = _full = False
        try:
            _blk.bind(("127.0.0.1", 0))          # 0 = 让系统给一个空闲端口
            _busy = _blk.getsockname()[1]
            _blk.listen(1)
            _hi = min(_busy + 10, 65535)
            _srv, _got = pick_port("127.0.0.1", _busy, _hi)
            _srv.server_close()
            _fall = _busy < _got <= _hi
            try:
                pick_port("127.0.0.1", _busy, _busy)   # 只给这一个、它还占着 → 必须抛
                _full = False
            except OSError:
                _full = True
        except OSError as _e:
            print("      （端口兜底自检没跑成：%s）" % _e)
        finally:
            _blk.close()
        check("端口兜底：真占住一个端口后就往上让（不是嘴上说说）", _fall)
        check("端口兜底：一个都起不来时抛 OSError —— 不静默返回", _full)
    finally:
        shutil.rmtree(_tm, ignore_errors=True)

    print("\n✓ 自检全部通过" if ok else "\n✗ 自检有失败项")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="秋招投递进度 + 邮箱通知看板")
    ap.add_argument("--config", default="config.ini", help="配置文件路径")
    # config.ini 里的相对路径（data/…）默认是相对**这个脚本所在的目录**解析的，
    # 不是当前工作目录。所以想拿别处的数据跑（比如 tools/make-demo-data.py 生成的
    # 脱敏演示数据），必须把 --base 指过去；只 cd 过去是不够的，那样读到的还是
    # 仓库里那份真实数据（2026-10-02 就这么把真实公司名拍进过 samples/ 截图）。
    ap.add_argument("--base", default="", help="数据根目录，默认是脚本所在目录")
    ap.add_argument("--serve", action="store_true",
                    help="起本地看板服务（默认端口 8788，被占就自动往上试到 8791）")
    ap.add_argument("--port", type=int, default=0,
                    help="端口，默认读 config.ini 或 8788；被占时自动在 8788–8791 里往上试")
    ap.add_argument("--no-open", action="store_true", help="起服务后不自动开浏览器")
    ap.add_argument("--add", action="store_true", help="命令行加一条投递记录")
    ap.add_argument("--company", default="", help="公司名称")
    ap.add_argument("--position", default="", help="岗位名称")
    ap.add_argument("--url", default="", help="网页链接")
    ap.add_argument("--status", default="已投递", help=f"状态，可选：{'/'.join(STATUSES)}")
    ap.add_argument("--note", default="", help="备注")
    ap.add_argument("--list", action="store_true", help="打印现有投递记录")
    ap.add_argument("--report", action="store_true", help="生成离线单文件看板")
    ap.add_argument("--notices-sync", action="store_true", help="立刻同步一次邮箱通知（飞书 / 邮箱直连）")
    ap.add_argument("--mail-config", action="store_true", help="配置邮箱直连（IMAP）：填邮箱 + 授权码，当场验登录")
    ap.add_argument("--mail-eml", default="", help="不联网，拿本地 .eml 文件把识别规则跑一遍（调规则用）")
    ap.add_argument("--opps-sync", action="store_true", help="立刻从飞书同步一次机会库（表很大，要等一会）")
    ap.add_argument("--import-apps", action="store_true",
                    help="从飞书表格导入历史投递记录（一次性，可重复跑，不会重复导入）")
    ap.add_argument("--apps-sync", action="store_true",
                    help="立刻从飞书同步一次投递进度（表里改了状态，看板跟着变）")
    ap.add_argument("--dry-run", action="store_true", help="配合 --import-apps：只看要导入什么，不写文件")
    ap.add_argument("--self-test", action="store_true", help="自检")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    # --base 指过去时，config.ini 优先在那个目录里找
    cfg_path = resolve_config_path(args.base, args.config)
    cfg = Config(cfg_path, base=(os.path.abspath(args.base) if args.base else None))

    if args.import_apps:
        if not (cfg.app_base and cfg.app_table):
            print("· config.ini 里没有 [applications] 段（feishu_base_token / feishu_table_id）。")
            print("  想导入的话，把飞书表格链接里 /base/ 和 ?table= 后面那两段填进去。")
            return 1
        print("· 正在读飞书表…")
        try:
            added, skipped, n_pending, pending = import_apps(cfg, dry_run=args.dry_run)
        except RuntimeError as e:
            print("✗ " + str(e))
            return 1
        head = "（演练，没写文件）" if args.dry_run else ""
        print("✓ 新增 %d 条%s，跳过 %d 条（已经在了）" % (added, head, skipped))
        print("→ " + cfg.app_csv)
        if n_pending:
            print("· 另外 %d 条状态是「未投递」，没混进投递进度（会算多总数），" % n_pending)
            print("  单独存到了： %s" % cfg.pending_csv)
            for r in pending[:10]:
                print("    · %s %s" % (r["公司名称"], r["岗位名称"]))
            if n_pending > 10:
                print("    · …还有 %d 条" % (n_pending - 10))
        return 0

    if args.apps_sync:
        if cfg.apps_source != "feishu":
            print("· 当前 [applications] source = %s，不是飞书，没什么可同步的。" % cfg.apps_source)
            print("  想同步的话，把飞书表格链接里 /base/ 和 ?table= 后面那两段填进 config.ini。")
            return 0
        print("· 正在读飞书投递表…")
        ok, msg = sync_apps(cfg, force=True)
        print(("✓ " if ok else "✗ ") + msg)
        if not ok:
            return 1
        print("→ " + cfg.app_csv)
        for c in _APP_LAST_CHANGES[:15]:
            print("    · %s %s：%s" % (c["公司名称"], c["岗位名称"], "；".join(c["diff"])))
        if len(_APP_LAST_CHANGES) > 15:
            print("    · …还有 %d 条改动" % (len(_APP_LAST_CHANGES) - 15))
        if not _APP_LAST_CHANGES:
            print("    · 没有需要变的地方（飞书那边没动过）")
        return 0

    if args.opps_sync:
        if cfg.opps_source != "feishu":
            print("· 当前 [opportunities] source = %s，不是飞书，没什么可同步的。" % cfg.opps_source)
            return 0
        print("· 正在拉飞书表（八九千条，十几页，大概 1 分钟）…")
        ok, msg = sync_opportunities(cfg, force=True)
        print(("✓ " if ok else "✗ ") + msg)
        if ok:
            print("→ " + cfg.opps_json)
        return 0 if ok else 1

    if args.mail_config:
        return run_mail_wizard(cfg, args.config if os.path.isabs(args.config)
                               else (cfg_path or args.config))

    if args.mail_eml:
        return mail_eml_report(cfg, args.mail_eml)

    if args.notices_sync:
        if cfg.notices_source not in ("feishu", "imap"):
            print("· 当前 [notices] source = %s，没接飞书 / 邮箱，没什么可同步的。" % cfg.notices_source)
            print("  想连邮箱：python3 applytracker.py --mail-config")
            return 0
        ok, msg = sync_notices(cfg, force=True)
        print(("✓ " if ok else "✗ ") + msg)
        if ok:
            print("→ " + cfg.notices_csv)
        return 0 if ok else 1

    if args.add:
        if not args.company:
            print("✗ 至少要给 --company")
            return 1
        rec, dup = add_app(cfg, args.company, args.position, args.url, args.status, args.note, "命令行")
        print(("· 这条已经记过了：" if dup else "✓ 已记下：") +
              f"{rec['公司名称']} | {rec['岗位名称']} | {rec['状态']} | {rec['投递时间']}")
        return 0

    if args.list:
        ensure_notices(cfg)
        apps = load_apps(cfg)
        attach_notices(apps, load_notices(cfg))
        if not apps:
            print("还没有投递记录。")
            return 0
        for a in sorted(apps, key=lambda x: x["投递时间"], reverse=True):
            env = a["关联"]
            print(f"{a['投递时间']}  {a['状态']:<6}  {a['公司名称']}  {a['岗位名称']}"
                  + (f"  [通知 {env['count']} 条]" if env["count"] else ""))
        return 0

    if args.report:
        ensure_notices(cfg)
        print(f"✓ 看板已生成 → {write_html(cfg)}")
        return 0

    # 没给别的子命令就起服务（start.command / 双击就跑这个）
    return serve(cfg, port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    sys.exit(main())
