#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成脱敏的演示数据 —— 给 samples/ 里的截图和离线看板样例用。

为什么要有这个文件：仓库要开源，但 samples/ 里的截图和「看板样例.html」
一旦用真实数据生成，就等于把某个人的投递记录、邮箱通知一起公布出去了。
所以演示数据全部在这里**编造**，跟任何真实公司、任何人无关。

用法：
    python3 tools/make-demo-data.py                 # 写到临时目录，并刷新 samples/投递记录样例.csv
    python3 tools/make-demo-data.py --out-dir DIR   # 指定输出目录（会建 DIR/data/）

出完数据之后，拿它去拍 samples/ 里的截图和离线样例（三条命令，顺序别换）：

    # 1) 起演示服务。**--base 和 --config 都必须带**：
    #    --base 是数据根目录（config.ini 里的相对路径按它解析，光 cd 过去没用）；
    #    --config 兜住「base 里没有 config.ini 时退回当前目录那份」这条退路 ——
    #    2026-10-02 两次踩雷都出在这儿：
    #      第一次只 cd 到演示目录 → 读到仓库里那份真实数据，真实公司名进了 6 张截图；
    #      第二次给了 --base，但当前目录也有 config.ini（仓库那份，带真实飞书 token），
    #      于是演示服务照真实 token 把**真实投递记录同步进了演示目录**，
    #      截图里直接出现真实公司名（两次都没提交，已还原）。
    python3 applytracker.py --serve --no-open --port 8790 \
        --base /tmp/at-demo --config /tmp/at-demo/config.ini
    # 2) 出截图（脚本自己会先验数据脱不脱敏，验不过就一个文件都不写）
    DEMO_DIR=/tmp/at-demo BROWSER_URL=http://127.0.0.1:9444 BASE=http://127.0.0.1:8790 \
        node tools/make-screenshots.js
    # 3) 出离线样例，再拷进仓库
    python3 applytracker.py --report --base /tmp/at-demo --config /tmp/at-demo/config.ini
    cp /tmp/at-demo/data/看板.html samples/看板样例.html

出的这几个文件跟真实运行时的完全同构：
    applications.csv      投递记录（字段直接引用主程序的 CSV_FIELDS，见 APP_FIELDS）
    notices.csv           邮箱通知（字段见 NOTICE_FIELDS）
    notice_states.json    看板上手标的处理状态（演示里留空，状态直接写在 CSV 里）
    待投递.csv            收藏了但还没投的清单
    opportunities.json    机会库缓存（synced_at / count / rows）
"""

import argparse
import csv
import io
import json
import os
import random
import shutil
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

STATUSES = ["已投递", "简历通过", "测评笔试", "面试", "Offer", "已结束"]
NOTICE_FIELDS = ["公司名称", "岗位名称", "通知类型", "通知截止时间", "邮件链接",
                 "邮件主题", "邮件收到时间", "处理状态", "发件人"]

# 投递记录的字段清单直接问主程序要，**不在这里手抄一份**。
# 2026-09-30 踩过这个坑：v0.15.0 给 applications.csv 加了「飞书状态」列，这份手抄的
# 副本没跟上 → samples/投递记录样例.csv 少一列，而 README 里写的字段是带「飞书状态」的。
# 抄出来的副本迟早会漂，所以这里改成引用。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import applytracker  # noqa: E402

APP_FIELDS = list(applytracker.CSV_FIELDS)

# 全部为虚构企业，跟真实公司无关
COMPANIES = [
    ("星桥科技", "hr.xingqiao-tech.com", "互联网/软件"),
    ("云溪智能", "campus.yunxi-ai.com", "人工智能"),
    ("优行科技", "job.youxing-mobility.com", "出行服务"),
    ("跃迁网络", "careers.yueqian.net", "互联网/软件"),
    ("南方云科", "hr.nanfang-cloud.com", "云计算"),
    ("思迈传媒", "join.simai-media.com", "文化传媒"),
    ("恒瑞达机械", "zhaopin.hengruida.com", "装备制造"),
    ("天工仪器", "hr.tiangong-inst.com", "精密仪器"),
    ("锐驰汽配", "campus.ruichi-auto.com", "汽车零部件"),
    ("同创电子", "job.tongchuang-elec.com", "消费电子"),
    ("鸿远建材", "hr.hongyuan-bm.com", "建筑材料"),
    ("锦程纺织", "join.jincheng-tex.com", "纺织服装"),
    ("青禾食品", "campus.qinghe-food.com", "食品饮料"),
    ("沃田乳业", "hr.wotian-dairy.com", "乳制品"),
    ("尚品服饰", "job.shangpin-style.com", "服装零售"),
    ("明轩家居", "careers.mingxuan-home.com", "家居零售"),
    ("乐橙文化", "hr.lecheng-cul.com", "文化传媒"),
    ("长风物流", "campus.changfeng-log.com", "物流运输"),
    ("万顺供应链", "hr.wanshun-scm.com", "供应链"),
    ("万象商贸", "job.wanxiang-trade.com", "批发零售"),
    ("弘毅咨询", "campus.hongyi-consult.com", "管理咨询"),
    ("晨光教育", "hr.chenguang-edu.com", "教育服务"),
    ("康泰健康", "join.kangtai-health.com", "医疗健康"),
    ("华宇新能源", "campus.huayu-energy.com", "新能源"),
    ("博远医药", "hr.boyuan-pharma.com", "医药制造"),
    ("嘉禾农业", "job.jiahe-agri.com", "现代农业"),
    ("润泽环保", "campus.runze-env.com", "环保工程"),
    ("宏图地产", "hr.hongtu-land.com", "房地产"),
    ("新程汽车", "job.xincheng-auto.com", "汽车制造"),
    ("海纳生物", "campus.haina-bio.com", "生物科技"),
]

# 机会库专用的公司池：在 COMPANIES 之外再补一批虚构企业。
#
# 为什么要另开一个池子、而且要这么多家：
#   ① 真实的机会库本来就有上千家公司，绝大多数你根本没投过 ——
#      它跟「投过的那二三十家」不是一回事，混在一个池子里反而不像真的。
#   ② 机会库是**按家分页**的，一页 60 家（applytracker.py：`oppLimit += 60`）。
#      公司总数不到 61 家时，「再看 60 家」那条翻页条根本不出现，
#      于是「翻页不把一家劈到两页上」这类断言**没有东西可测**（不是坏了，是瞎了）。
#      2026-10-08 实测：30 家公司造 60 条机会，落到页面上只有 25 家 → 六条断言里两条直接瞎掉。
#   ③ 单独用一条自己的随机数序列（main 里 `rng_opps`），
#      这样改机会库不会搅动 applications / notices / pending 的取值，
#      samples/投递记录样例.csv 也就不会跟着变。
OPP_COMPANIES = COMPANIES + [
    ("沐光照明", "hr.muguang-light.com", "消费电子"),
    ("恒通水务", "job.hengtong-water.com", "环保工程"),
    ("星野文创", "campus.xingye-cul.com", "文化传媒"),
    ("远山新材", "hr.yuanshan-mat.com", "建筑材料"),
    ("蓝汐网络", "job.lanxi-net.com", "互联网/软件"),
    ("拓普机器人", "campus.tuopo-robot.com", "装备制造"),
    ("翰林出版", "hr.hanlin-press.com", "文化传媒"),
    ("佳合化工", "job.jiahe-chem.com", "化工制造"),
    ("灵犀数据", "careers.lingxi-data.com", "互联网/软件"),
    ("瑞丰粮油", "hr.ruifeng-grain.com", "食品饮料"),
    ("启明半导体", "job.qiming-semi.com", "消费电子"),
    ("晟远通信", "campus.shengyuan-tel.com", "通信设备"),
    ("锦澜酒店", "hr.jinlan-hotel.com", "批发零售"),
    ("润禾种业", "job.runhe-seed.com", "现代农业"),
    ("昊天重工", "hr.haotian-heavy.com", "装备制造"),
    ("景泰文旅", "campus.jingtai-travel.com", "文化传媒"),
    ("凌云航空", "job.lingyun-aero.com", "装备制造"),
    ("维康医疗", "hr.weikang-med.com", "医疗健康"),
    ("博涛软件", "campus.botao-soft.com", "互联网/软件"),
    ("安泰保险", "job.antai-ins.com", "金融服务"),
    ("汇智人力", "hr.huizhi-hr.com", "管理咨询"),
    ("格物检测", "job.gewu-test.com", "环保工程"),
    ("汇通快递", "campus.huitong-ex.com", "物流运输"),
    ("文轩图书", "hr.wenxuan-book.com", "批发零售"),
    ("金石矿业", "job.jinshi-mine.com", "建筑材料"),
    ("清源环保", "campus.qingyuan-ep.com", "环保工程"),
    ("南岭林业", "hr.nanling-forest.com", "现代农业"),
    ("德茂家具", "job.demao-furn.com", "家居零售"),
    ("领航体育", "campus.linghang-sport.com", "服装零售"),
    ("香榭烘焙", "hr.xiangxie-bake.com", "食品饮料"),
    ("普惠金融", "campus.puhui-fin.com", "金融服务"),
    ("明德学校", "job.mingde-school.com", "教育服务"),
    ("广济医械", "hr.guangji-device.com", "医疗健康"),
    ("智行芯片", "campus.zhixing-chip.com", "消费电子"),
    ("中远船务", "job.zhongyuan-ship.com", "装备制造"),
    ("云图测绘", "hr.yuntu-geo.com", "管理咨询"),
    ("嘉沃农科", "campus.jiawo-agri.com", "现代农业"),
    ("朗华制药", "job.langhua-pharm.com", "医药制造"),
    ("筑城建设", "hr.zhucheng-build.com", "房地产"),
    ("风行传媒", "campus.fengxing-media.com", "文化传媒"),
    ("海翼风电", "job.haiyi-wind.com", "新能源"),
    ("泰和锂电", "hr.taihe-battery.com", "新能源"),
    ("求是科研", "campus.qiushi-lab.com", "生物科技"),
]

POSITIONS = [
    "市场营销岗", "电商运营", "消费者与市场洞察", "采购管培生", "品牌传播",
    "渠道运营", "产品运营", "供应链管培生", "人力资源管理培训生", "财务管培生",
    "新媒体运营", "客户经理", "数据分析岗", "综合管理培训生", "商务拓展",
    "店铺运营", "视觉设计师", "销售工程师", "市场推广专员", "运营支持专员",
]

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "武汉", "成都", "南京", "西安", "长沙"]
NOTICE_TYPES = ["测评", "AI面", "面试", "笔试", "资料补充", "Offer"]

# 「面试进度」那块要演示得完整，通知就不能随机撒。
# 这条是推进路线：第 i 家公司一路发到第 i 档，于是进度条有长有短 ——
# 测评 / AI面试 / 一面 / 二面 / 终面 / offer，「已达成的 + 当前 + 还没到的灰块」三种状态一屏都有。
NOTICE_CHAIN = [
    ("测评", "请参加%s2027校园招聘在线测评"),
    ("AI面", "%s2027校园招聘AI面试通知"),
    ("面试", "%s邀请你参加校招初试"),
    ("面试", "%s校招复试安排通知"),
    ("面试", "%s校招终面邀请"),
    ("Offer", "%s2027校园招聘录用意向通知"),
]

# 投递日期：含本周（周一二三）、上周、以及更早，便于看板展示「投递节奏」
DATES = [
    "2026-09-30 10:12", "2026-09-29 19:38", "2026-09-29 15:41", "2026-09-28 11:07",
    "2026-09-27 20:26", "2026-09-26 18:14", "2026-09-25 16:33", "2026-09-24 09:48",
    "2026-09-23 21:05", "2026-09-22 14:20", "2026-09-21 10:31", "2026-09-20 17:52",
    "2026-09-18 13:14", "2026-09-17 09:26", "2026-09-16 15:44", "2026-09-15 11:08",
    "2026-09-14 19:03", "2026-09-12 10:55", "2026-09-11 16:21", "2026-09-10 09:37",
    "2026-09-09 14:12", "2026-09-08 20:41", "2026-09-07 11:29", "2026-09-05 15:58",
    "2026-09-04 10:16", "2026-09-03 17:43", "2026-09-02 09:52", "2026-09-01 13:35",
    "2026-08-31 18:20", "2026-08-29 11:14", "2026-08-28 15:06", "2026-08-26 09:44",
    "2026-08-25 16:38", "2026-08-24 10:21", "2026-08-22 14:57", "2026-08-21 19:12",
    "2026-08-20 09:18", "2026-08-19 15:33", "2026-08-18 11:46", "2026-08-17 17:09",
    "2026-08-14 10:27", "2026-08-12 16:50", "2026-08-11 09:35", "2026-08-08 14:04",
]

# 状态分布：让顶部五个数字都不是 0，截图看着才完整
STATUS_PLAN = (["已投递"] * 18 + ["简历通过"] * 4 + ["测评笔试"] * 7
               + ["面试"] * 5 + ["Offer"] * 2 + ["已结束"] * 6)

NOTES_POOL = ["", "", "", "", "内推码 YZ2027", "官网直投", "宣讲会现场投递",
              "已过初筛", "HR说10月统一安排面试", "需要现场笔试", "岗位改过名称"]

# 机会库的截止时间：掺一部分**已经过期**的。
# 为什么必须掺：默认就开着「还没截止」这个筛选，演示数据里要是**一条过期的都没有**，
# 筛完跟没筛是同一个数，页面就只写「共 N 家」而不写「筛出 N 家（共 M 家）」——
# 于是「计数口径一致」「如实标注有筛选在生效」这两条断言天然测不出东西。
# （2026-10-08 实测：60 条机会全都没过期 → 这两条假红。）
# 参考今天：演示数据里的「今天」按 2026-10-08 算。
DEADLINE_OPEN = ["尽快投递", "2026-10-15", "2026-10-31", "2026-11-20"]
DEADLINE_PAST = ["2026-09-12", "2026-09-26", "2026-10-02", "2026-10-06"]

# 招聘对象：掺一部分**不含 2027 届**的。
# 同理：全表都能投的话，KPI 卡「2027 届能投」点下去结果一个不变，
# 「点一下 → 点亮且结果变化」这条断言就测不出来。
TARGETS_MAIN = ["2026届", "2027届"]
TARGETS_NO_2027 = [["2026届"], ["2025届", "2026届"], ["2028届"]]


def _dl_ts(s):
    """把截止时间文本折成 epoch 秒，跟真实数据一个口径。

    为什么不能省：机会库缓存（opportunities.json）里的行是**原样使用**的，
    服务端不会再拿「截止时间」文本重新算一遍 —— 缓存里写的是 null，
    「还没截止」这个默认筛选就一条都筛不掉，页面永远显示「共 N 家」，
    「筛出 N 家（共 M 家）」那套计数口径的断言全是瞎的。
    （2026-10-08 实测踩到：加了过期日期但 dl 没给，假红一条没少。）
    """
    if not s or not s[:1].isdigit():
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").timestamp()
    except ValueError:
        return None


def _write_csv(path, fields, rows):
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def build_applications(rng):
    rows = []
    statuses = list(STATUS_PLAN)
    rng.shuffle(statuses)
    used = set()
    for i, when in enumerate(DATES):
        while True:
            company, host, _ind = rng.choice(COMPANIES)
            position = rng.choice(POSITIONS)
            if (company, position) not in used:
                used.add((company, position))
                break
        status = statuses[i] if i < len(statuses) else "已投递"
        slug = rng.choice(["campus", "job", "apply", "position"])
        rows.append({
            "id": "%08x" % rng.getrandbits(32),
            "公司名称": company,
            "岗位名称": position,
            "状态": status,
            # 「飞书状态」存的是飞书表格里那格的**原文**，这里照同构造假
            "飞书状态": "" if rng.random() < 0.35 else status,
            "投递时间": when,
            "网页链接": "https://%s/%s/%d" % (host, slug, 1000 + i),
            "来源": rng.choice(["飞书表"] * 6 + ["扩展"] * 3 + ["手动"]),
            "备注": rng.choice(NOTES_POOL),
            "更新时间": when,
        })
    return rows


def _notice(rng, a, ntype, subject, step, i):
    """一条通知。step 越大 = 越靠后的一轮，收到时间也越晚，
    这样「最近一条通知」正好是最后那一轮（看板就是靠这个判断当前走到哪）。"""
    when = datetime.strptime(a["投递时间"], "%Y-%m-%d %H:%M") + timedelta(days=step + 1)
    ymd = when.strftime("%Y-%m-%d")
    return {
        "公司名称": a["公司名称"],
        "岗位名称": a["岗位名称"],
        "通知类型": ntype,
        "通知截止时间": (when + timedelta(days=3)).strftime("%Y-%m-%d %H:%M"),
        "邮件链接": "https://bsurl.example.com/v2/demo%03d" % i,
        "邮件主题": subject % a["公司名称"],
        "邮件收到时间": "%s %02d:%02d" % (ymd, rng.randint(9, 20), rng.randint(0, 59)),
        "处理状态": rng.choice(["待处理"] * 3 + ["已完成"] * 5 + ["已过期", "已忽略"]),
        "发件人": "%s校园招聘" % a["公司名称"],
    }


def build_notices(rng, apps):
    rows = []
    picks = rng.sample(apps, 12)
    # 各家公司推到第几档：4 家刚到测评、2 家 AI 面、2 家一面、2 家二面、1 家终面、1 家 offer ——
    # 进度条长什么样一屏全能看到（全是 offer 的演示反而看不出这条进度条在说什么）
    depths = [0, 0, 1, 1, 2, 2, 3, 3, 4, 5, 0, 1]
    for i, a in enumerate(picks):
        for step in range(depths[i] + 1):
            ntype, tpl = NOTICE_CHAIN[step]
            rows.append(_notice(rng, a, ntype, tpl, step, len(rows)))
    # 再补几条别的类型，让「通知类型分布」那块不是只有测评 / 面试
    for a in rng.sample(picks, 4):
        ntype = rng.choice(["笔试", "资料补充"])
        rows.append(_notice(rng, a, ntype, "%s2027校园招聘" + ntype + "通知", 0, len(rows)))
    rows.sort(key=lambda r: r["邮件收到时间"], reverse=True)
    return rows


def build_pending(rng):
    rows = []
    for i in range(9):
        company, host, _ind = rng.choice(COMPANIES)
        rows.append({
            "id": "%08x" % rng.getrandbits(32),
            "公司名称": company,
            "岗位名称": rng.choice(POSITIONS),
            "状态": "未投递",
            "飞书状态": "",
            "投递时间": "",
            "网页链接": "https://%s/campus/%d" % (host, 2000 + i),
            "来源": "飞书表",
            "备注": rng.choice(CITIES),
            "更新时间": "2026-09-30 10:00",
        })
    return rows


def build_opportunities(rng):
    rows = []
    # 机会库是**按家分页**的，一页 60 家（applytracker.py：`oppLimit += 60`）。
    # 「翻下一批后每一家照样整家在」这条断言要能测出东西，**多岗位的公司必须多过一页**：
    # 不足 60 家时，翻页前后家数一个不变，断言等于瞎掉（2026-10-08 实测踩到：
    # 160 条机会撒在 73 家上，多岗位的只有 51 家 → 批1 34 家 / 批2 34 家，假红）。
    # 所以这里规划着来：前 64 家每家至少 2 条，剩下的条数撒给后面的公司当单条。
    N_MULTIPAGE = 64
    plan = []
    for ci in range(N_MULTIPAGE):
        plan.append((ci, True))     # 这一条**必须没过期** —— 一家的条数全过期，
        plan.append((ci, False))    # 整家会被默认的「还没截止」筛掉，翻页又不够 60 家了
    while len(plan) < 160:
        plan.append((rng.randrange(N_MULTIPAGE, len(OPP_COMPANIES)), False))
    rng.shuffle(plan)

    for i, (ci, force_open) in enumerate(plan):
        company, host, ind = OPP_COMPANIES[ci]
        dl_str = (rng.choice(DEADLINE_OPEN) if force_open
                  else (rng.choice(DEADLINE_PAST) if rng.random() < 0.35
                        else rng.choice(DEADLINE_OPEN)))
        rows.append({
            "id": "demo%04d" % i,
            "company": company,
            "industries": [ind],
            "nature": [rng.choice(["央企", "国企", "民企", "外企"])],
            "nat": [rng.choice(["央企", "国企", "民企", "外企"])],
            "cities": rng.sample(CITIES, rng.randint(1, 4)),
            "roles": "".join(rng.sample(POSITIONS, 3)),
            "roles_more": 0,
            "deadline": dl_str,
            "apply": "https://%s/campus" % host,
            "notice": "https://mp.weixin.example.com/s/demo%03d" % i,
            "targets": (rng.choice(TARGETS_NO_2027) if rng.random() < 0.3
                        else list(TARGETS_MAIN)),
            "batch": [rng.choice(["秋招", "秋招", "春招"])],
            "exam": [rng.choice(["需要笔试", "无需笔试"])],
            "note": company,
            "updated": "2026/09/30",
            "dl": _dl_ts(dl_str),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="/tmp/at-demo",
                    help="输出目录，会建 <out-dir>/data/（默认 /tmp/at-demo）")
    ap.add_argument("--seed", type=int, default=20270930)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    data_dir = os.path.join(args.out_dir, "data")
    if os.path.isdir(args.out_dir):
        shutil.rmtree(args.out_dir)
    os.makedirs(data_dir)

    apps = build_applications(rng)
    notices = build_notices(rng, apps)
    pending = build_pending(rng)
    # 机会库单独一条随机数序列：改它不会搅动上面那三份的取值
    # （applications 会顺带写进 samples/投递记录样例.csv，不该因为动机会库就变）
    opps = build_opportunities(random.Random(args.seed + 101))

    _write_csv(os.path.join(data_dir, "applications.csv"), APP_FIELDS, apps)
    _write_csv(os.path.join(data_dir, "notices.csv"), NOTICE_FIELDS, notices)
    _write_csv(os.path.join(data_dir, "待投递.csv"), APP_FIELDS, pending)
    with io.open(os.path.join(data_dir, "notice_states.json"), "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False)
    with io.open(os.path.join(data_dir, "opportunities.json"), "w", encoding="utf-8") as f:
        json.dump({"synced_at": "2026-09-30 10:00", "count": len(opps), "rows": opps},
                  f, ensure_ascii=False, indent=1)

    # 演示目录要自带一份 config.ini：否则「通知」会被当成「没配置」，
    # 跑去看隔壁 qqmail2table 的输出，页面上的通知区就是空的。
    #
    # 三个 source **必须都是本地**：
    #   [notices]       source = file   —— 通知读 data/notices.csv
    #   [applications]  source = off    —— 投递记录只读 data/applications.csv，**不许连飞书**
    #   [opportunities] source = off    —— 机会库读 data/opportunities.json 缓存
    # 2026-10-02 的教训：原来这里写的是 `source = file`（applications 段不认这个值，
    # 会一路退到默认的 feishu），演示服务就照着真实 token 把真实投递记录同步了进来。
    # 写错一个词，拍出来的就是要开源的截图里出现真实公司名 —— 所以这里只写主程序
    # 认得的取值，别图省事。
    with io.open(os.path.join(args.out_dir, "config.ini"), "w", encoding="utf-8") as f:
        f.write(
            "# 演示用配置：只读本地这几个文件，不连任何飞书表。\n"
            "\n"
            "[notices]\n"
            "source = file\n"
            "csv = data/notices.csv\n"
            "\n"
            "[opportunities]\n"
            "source = off\n"
            "\n"
            "[applications]\n"
            "source = off\n"
        )

    # 仓库里那份小样例表也用同一批虚构数据的前几条，保持口径一致
    sample_csv = os.path.join(ROOT, "samples", "投递记录样例.csv")
    if os.path.isdir(os.path.dirname(sample_csv)):
        _write_csv(sample_csv, APP_FIELDS, apps[:12])
        print("已刷新 %s" % os.path.relpath(sample_csv, ROOT))

    print("演示数据已生成 → %s" % data_dir)
    print("  投递 %d 条 / 通知 %d 条 / 待投递 %d 条 / 机会 %d 条"
          % (len(apps), len(notices), len(pending), len(opps)))
    print("  公司名单全部为虚构，不含任何真实企业")
    return 0


if __name__ == "__main__":
    sys.exit(main())
