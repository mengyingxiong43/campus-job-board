#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读本机「信息」App 的短信库，把校招的面试/测评短信挑出来。

背景：iPhone 开了「设置 → 信息 → 短信转发 → 这台 Mac」之后，106 开头的企业短信
（也就是公司的测评/面试通知）会同时落到 Mac 上，存进 `~/Library/Messages/chat.db`。
这个脚本就是去那个库里把这类短信捞出来。

⚠️ 只读，一个字节都不写回去。也不往任何地方发。

权限（两个都要，缺一个都读不到）：
  ① iPhone 上开「短信转发」；
  ② 跑这个脚本的 App（WorkBuddy / 终端）在
     「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里被勾上。
     没勾上时的报错长这样：`PermissionError: [Errno 1] Operation not permitted`。

用法：
    python3 tools/sms-probe.py                     # 概览 + 最近 20 条
    python3 tools/sms-probe.py -n 50               # 最近 50 条
    python3 tools/sms-probe.py --grep 测评,面试      # 只看含这些词的（默认给的就是校招那批）
    python3 tools/sms-probe.py --all               # 连 iMessage 里的也算进来
    python3 tools/sms-probe.py --json /tmp/sms.json  # 导出成 JSON，给下一步识别用
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# 校招短信里会出现的词。挑得**故意宽**：这一步只是「捞出候选」，
# 真正判「是测评还是面试、是哪家公司」是下一步的事，这里宁可多捞。
DEFAULT_GREP = [
    "测评", "笔试", "面试", "在线考试", "网申", "校招", "校园招聘", "秋招", "春招",
    "简历", "初筛", "终面", "一面", "二面", "AI面", "复试", "offer", "录用", "录取",
    "宣讲", "招聘",
]

# 【美团】这种签名 —— 企业短信的正文开头就是公司名，这是最可靠的抓手。
SIGN_RE = re.compile(r"【([^】]{1,20})】")

APPLE_EPOCH = datetime(2001, 1, 1)


def apple_ts(v):
    """Apple 的时间戳：老系统是秒，新系统是纳秒（差 9 个数量级，只能靠量级猜）。"""
    if not v:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    if v > 10 ** 14:            # 纳秒
        v = v / 1e9
    elif v > 10 ** 11:          # 微秒（极少数版本）
        v = v / 1e6
    return APPLE_EPOCH + timedelta(seconds=v)


def open_db(path):
    if not os.path.exists(path):
        raise SystemExit(
            "找不到 %s\n"
            "→ 这台 Mac 上「信息」App 还没被用过，或者路径不对。" % path)
    # 只读打开；不加 immutable —— 那个会忽略 -wal，最近收到的短信就看不见了。
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
        con.execute("select count(*) from message").fetchone()
        return con
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        raise SystemExit(
            "打不开短信库（%s）。\n"
            "→ 十有八九是**没给完全磁盘访问权限**。\n"
            "   系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 打开 WorkBuddy（以及终端），"
            "然后重开一次。" % e)


def cols(con, table):
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.DatabaseError:
        return set()


def fetch(con, want_imessage, greps, limit):
    have = cols(con, "message")
    has_service = "service" in have
    sql = [
        "SELECT m.ROWID, m.date, m.text, m.attributedBody,",
        ("m.service" if has_service else "''") + " AS service,",
        "h.id AS sender",
        "FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID",
        "WHERE m.is_from_me = 0",
    ]
    if not want_imessage and has_service:
        sql.append("AND m.service = 'SMS'")
    sql.append("ORDER BY m.date DESC")
    rows = con.execute(" ".join(sql)).fetchall()

    out = []
    for rowid, date, text, blob, service, sender in rows:
        body = text or ""
        if not body and blob:
            # 有些短信正文只存在 attributedBody 这个二进制里（富文本）。
            # 里面其实就是一段 bplist，正文以明文夹在中间 —— 直接抠可打印片段，够用了。
            try:
                body = bytes(blob).decode("utf-8", "ignore")
                body = "".join(ch for ch in body if ch == "\n" or ch >= " ")
            except Exception:
                body = ""
        body = (body or "").strip()
        if greps:
            hay = body.lower()
            if not any(g.lower() in hay for g in greps):
                continue
        sign = SIGN_RE.findall(body)
        out.append({
            "rowid": rowid,
            "time": (apple_ts(date) or datetime(1970, 1, 1)).strftime("%Y-%m-%d %H:%M"),
            "sender": sender or "",
            "service": service or "",
            "签名": sign[0] if sign else "",
            "text": body,
        })
        if limit and len(out) >= limit:
            break
    return out


def selftest():
    """造一个假短信库，验「只挑短信、不挑 iMessage；只挑校招的、不挑广告和私人短信」。
    跑它不需要完全磁盘访问权限 —— 这是这个脚本唯一能自证的部分。"""
    import tempfile
    d = tempfile.mkdtemp(prefix="smsprobe-")
    p = os.path.join(d, "chat.db")
    con = sqlite3.connect(p)
    con.executescript(
        "create table handle(ROWID integer primary key, id text);"
        "create table message(ROWID integer primary key, date integer, text text,"
        " attributedBody blob, service text, is_from_me integer, handle_id integer);"
        "insert into handle values (1,'106550000000'),(2,'+8613800000000'),(3,'me@example.com');")
    base = datetime(2001, 1, 1)
    ts = lambda y, mo, d_, h, mi: int(
        (datetime(y, mo, d_, h, mi) - base).total_seconds() * 1e9)   # 纳秒口径
    for sql, args in [
        ("insert into message values (1,?,'【美团】您已通过2027届校园招聘简历初筛，"
         "请于10月16日12:00前完成在线测评：https://x/t 逾期作废。',NULL,'SMS',0,1)",
         (ts(2026, 10, 10, 9, 12),)),
        ("insert into message values (2,?,'【招商银行】面试通知：请您于10月18日14:30 参加面试。',"
         "NULL,'SMS',0,1)", (ts(2026, 10, 11, 10, 5),)),
        ("insert into message values (3,?,'今晚回来吃饭吗',NULL,'SMS',0,2)",
         (ts(2026, 10, 11, 11, 0),)),
        ("insert into message values (4,?,'【某电商】双11大促全场5折！',NULL,'SMS',0,1)",
         (ts(2026, 10, 11, 11, 30),)),
        ("insert into message values (5,?,'好',NULL,'iMessage',0,3)",
         (ts(2026, 10, 11, 11, 40),)),
        ("insert into message values (6,?,'我发的',NULL,'SMS',1,1)",
         (ts(2026, 10, 11, 11, 50),)),
    ]:
        con.execute(sql, args)
    con.commit()

    fails = []

    def ok(name, cond, extra=""):
        print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "   " + str(extra)))
        if not cond:
            fails.append(name)

    hits = fetch(con, False, DEFAULT_GREP, 0)
    names = [h["签名"] for h in hits]
    ok("只挑出「校招短信」那两条", len(hits) == 2, names)
    ok("【】里的就是公司名", names == ["招商银行", "美团"], names)
    ok("时间解成当地年月日（纳秒口径）", hits[1]["time"] == "2026-10-10 09:12", hits[1]["time"])
    ok("自己发的不算", all(h["sender"] != "" for h in hits))
    ok("广告（双11）不要", "某电商" not in names, names)
    ok("私人短信（吃饭）不要", all("吃饭" not in h["text"] for h in hits))
    ok("iMessage 不要（默认只看 SMS）",
       all(h["service"] == "SMS" for h in hits), [h["service"] for h in hits])
    allm = fetch(con, True, [], 0)
    ok("--all 时才把 iMessage 收进来", len(allm) == 5, len(allm))
    con.close()
    print("\n%s 假短信库自检：%d 项，失败 %d 项" % ("✓" if not fails else "✗", 8, len(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("-n", "--limit", type=int, default=20, help="最多打印几条（默认 20）")
    ap.add_argument("--grep", default=",".join(DEFAULT_GREP),
                    help="只看含这些词的短信，逗号分隔；给空串 = 全都要")
    ap.add_argument("--all", action="store_true", help="连 iMessage 一起算（默认只看短信）")
    ap.add_argument("--json", dest="out", help="导出 JSON 到这个路径")
    ap.add_argument("--db", default=CHAT_DB, help="短信库路径（默认 ~/Library/Messages/chat.db）")
    ap.add_argument("--selftest", action="store_true",
                    help="造个假短信库验筛选逻辑（不需要完全磁盘访问权限）")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())

    con = open_db(a.db)

    have = cols(con, "message")
    total = con.execute("select count(*) from message where is_from_me = 0").fetchone()[0]
    bysvc = {}
    if "service" in have:
        for s, n in con.execute(
                "select service, count(*) from message where is_from_me = 0 group by service"):
            bysvc[s or "(空)"] = n

    greps = [g for g in (a.grep or "").split(",") if g.strip()]
    hits = fetch(con, a.all, greps, a.limit)

    print("短信库：%s" % a.db)
    print("收到的消息共 %d 条%s" % (total, ("，按类型：" + json.dumps(bysvc, ensure_ascii=False))
                                    if bysvc else ""))
    print("其中命中关键词的：%d 条（下面最多列 %d 条）" % (len(hits), a.limit))
    print("关键词：%s" % ("、".join(greps) if greps else "(不筛，全都要)"))
    print("-" * 72)
    for h in hits:
        head = "[%s] %s%s" % (h["time"], h["sender"],
                              ("　【%s】" % h["签名"]) if h["签名"] else "")
        print(head)
        body = h["text"].replace("\n", " ")
        print("    " + (body[:180] + ("…" if len(body) > 180 else "")))
    if not hits:
        print("(一条都没有)")
        print()
        print("排查顺序：① iPhone 上「短信转发」开了吗？"
              "② 这台 Mac 的「信息」App 登的是不是同一个 Apple ID、"
              "③ 授权之后有没有重开过 WorkBuddy？")

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(hits, f, ensure_ascii=False, indent=1)
        print("\n已导出 %d 条 → %s" % (len(hits), a.out))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
