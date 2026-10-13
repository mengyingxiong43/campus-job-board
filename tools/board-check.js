// 看板体检：把「自己看着没问题、换个窗口宽度才露出来」的那类毛病量出来。
// 目前管十件事 ——
//   ① 筛选栏控件有没有互相压（条件挂多了 / 窗口窄了才会露出来，肉眼扫一屏看不出的）
//   ② 计数口径对不对（右下角「筛出 N / 共 M 条」与 KPI「筛出岗位」卡必须是同一个数）
//   ③ 主要交互还能用（KPI 卡点选、排序、最近新开、条件增删、分布面板、备注可编辑）
//   ④ 投递进度页：同公司多岗位折叠 + 按公司计数 + 整行可点 + **整行可改**
//      （公司名 / 岗位名 / 投递时间 / 链接 点一下都进编辑态、带出原值，发的字段名对不对；
//       编辑框是**浮在格子上**的 —— 顺带量一下它有没有把列宽挤走）
//   ⑤ 配色切换（自动 / 浅色 / 深色）：属性、真实颜色、手工选择压过系统偏好、刷新不闪
//   ⑥ 邮箱页的两个视图（面试测评截止 / 投递进度）：默认进哪一版、互斥显示对不对、
//      通知表是不是回到 9 列、进度视图里哪些公司在（只停在测评的不该在）、点一下能不能推进、
//      **点已经过的环节能不能退回来**、「改」能不能展开、980~1512px 挤不挤爆、
//      **筛选条（搜索 / 按环节筛 / 按「已止步」筛 / 记住选择）、一键止步只发 stop**
//   ⑥c 邮箱连接状态常驻 + 连接弹卡（她 2026-10-08 报「连完了这玩意儿还在」）：
//      弹卡加载后**不可见**（只读 el.hidden 会假绿，必须看 computedStyle）、点取消真消失、
//      状态行按「连没连」二分文案、两个把手各只发一次对应请求、1080/980px 不溢出
//   ⑥d 「按公司归纳」的公司行必须**横跨整行**（flex 挂 `td[colspan]` 会让 colspan 失效，
//      实测宽度塌成 219/1050 —— 眼睛扫一眼会当成「就这么设计的」，所以必须量）
//   ⑥e 平铺形态的**分段标题行**同理（同一条铁律的另一半，`tr.sect td` 一直被写成 flex）
//   ⑥f 「筛出 0」必须说清是**哪几个条件**筛没的 + 就地一键清除
//      （她 2026-10-09 报「你又给我漏邮件了」的真因：类型标签是持久化的，她早先点过的那个还挂着）
//   ⑥g 「标了已完成、但截止还没到」单独成段、不灰、照常报剩余天数，而且**段头与底下的行是同一撮**
//      （同一天她**第二次**报「漏」的现场：当天 21:00 截止的笔试通知 / 次日 10:21 截止的测评通知，
//       两条都被她自己的「已完成」标记藏进了灰区；段头错位那次是这种「既待处理又已完成未到期」
//       的公司把段头提前引走了，后面两家「没识别出截止」的白白掉进标题底下）
//   ⑦ 那排筛选 + 顶部精简：筛选一件不少还真管用、顶部只剩 3 个数字 1 块分布、
//      处理掉的不再报「过期 N 天」（但「标了已完成、截止还没到」那撮要照常报剩余天数 —— 见 ⑥g）
//   ⑧ 页面自己发现「我这份代码过期了」：版本对不上就亮提示条、她没在敲东西时自动刷新一次
//      （用打桩的 verDoReload 验 —— 真刷新的话后面的用例全废）、改着格子绝不刷、对上就收回去
//   ⑨ 机会库按公司归并：一家只占一行（分行收进主行）、点一下摊开、点标签只筛不展开、
//      计数同时说清「家数 + 条数」、**翻页不把一家劈到两页**、1080/980px 不横向溢出
//   ⑩ 投递进度表「并入 / 拆出」：每家公司行都有一颗按钮（含主公司那行）、点它只开面板
//      **不把整行摊开**、发的字段就是 child/parent、归并后顶层多一行主公司 / 被并那家从高层消失 /
//      顶层不出现重名、点开有形有「拆出」、拆掉能回到原样、1080/980px 不横向溢出
//   ⑩b **拖动并入**（合成的 DragEvent 走完整拖放）：只有「一家一行」的行能拖 / 岗位子行不能拖、
//      拖起来源行淡落点描圈、拖到自己身上不算、松手只发一次 child+parent、落点那家升成主公司行
//      且自动摊开、**成环要拦住**（不发请求）、从按钮上按下不算拖、拖动中撞上轮询不重建表格、
//      拖出来的那对老路子照样拆得掉
// 只读：全程不点「检查更新」，也不发任何记账类写请求（④ 会故意点几下「整行可改」→
// /api/status，⑥ 会故意点「推进 / 回退环节」→ /api/interview，⑩ / ⑩b 会故意「并入 / 拆出 / 拖动」→
// /api/app-parent；三处都按**次数**断言，而且所有写请求都被拦成假响应，一个字节都没落到她的数据上）。
//
// 前置：一个在跑的看板服务 + 一个可连的浏览器调试端口。
//   1) python3 applytracker.py --serve --no-open
//   2) ~/.workbuddy/bin/safe-browser serve --port 9444
//   3) BROWSER_URL=http://127.0.0.1:9444 node tools/board-check.js
//
// 依赖 puppeteer-core（不下载浏览器，只连现成的）：
//   PUPPETEER_DIR=/path/to/node_modules node tools/board-check.js
//
// 环境变量：BASE（默认 http://127.0.0.1:8788）、WIDTHS（默认 740..1440 一串）
// 退出码 0 = 全过，1 = 有失败项（能直接挂进 CI）。
//
// 数据：对着**自己的服务**跑（8788，真实数据）。①②③ 里几条计数断言跟数据有关 ——
// 演示数据（tools/make-demo-data.py）那 60 条机会刚好全都满足默认筛选，
// 切筛选时计数不变，会假红 4 条（不是坏了）。

const puppeteer = (() => {
  const path = require('path');
  const dirs = [process.env.PUPPETEER_DIR,
                process.env.HOME && path.join(process.env.HOME, '.workbuddy/binaries/node/workspace/node_modules')]
    .filter(Boolean);
  for (const d of dirs) { try { return require(path.join(d, 'puppeteer-core')); } catch (e) { /* 继续找 */ } }
  try { return require('puppeteer-core'); } catch (e) {
    console.error('找不到 puppeteer-core。先 npm i puppeteer-core，或用 PUPPETEER_DIR 指过去。');
    process.exit(1);
  }
})();

const BASE = process.env.BASE || 'http://127.0.0.1:8788';
const WIDTHS = (process.env.WIDTHS || '1440,1280,1100,1065,1024,980,940,900,860,820,780,740')
  .split(',').map(Number);
// 这档是实测最容易压在一起的（输入框下限 + 行换行阈值正好卡在这儿），单独重点看
const FOCUS = 980;

// ⚠️ 硬闸：体检**绝不许**改到她的 config.ini（授权码、数据源都在里面）。
// 2026-10-09 抓到一次真实事故：⑥c 会点「断开」→ POST /api/mail-off，正常情况下被下面那层
// fetch 打桩挡成假响应；但只要那个页面**没装到桩**（换了页面、复用上一轮留下的旧标签页、
// 将来重构把 fresh() 改成 newPage()），这一下就会**真写**她的 config.ini —— 现场就是
// [mail] source 被翻成了 off、面板文案从「已连上…（备用直连，没在用）」变成「邮箱直连已断开」。
// 所以：跑之前按一次指纹，跑完再按一次，不一致直接红 —— 不等她发现。
const _fs = require('fs');
const _pth = require('path');
const CONFIG_FILE = process.env.CONFIG_FILE || _pth.join(__dirname, '..', 'config.ini');
function configFingerprint() {
  try {
    return require('crypto').createHash('sha256')
      .update(_fs.readFileSync(CONFIG_FILE)).digest('hex').slice(0, 16);
  } catch (e) { return '(读不到 ' + (e && e.code) + ')'; }
}
const CFG_BEFORE = configFingerprint();

let pass = 0;
const fails = [];
const check = (name, ok, detail) => {
  if (ok) { pass++; console.log('  ✓ ' + name); }
  else { fails.push(name + (detail ? '  ← ' + detail : '')); console.log('  ✗ ' + name + (detail ? '  ← ' + detail : '')); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 浏览器与页面提到外层，好让末尾的 shutdown() 够得着 —— 跑崩时也要关页面（见文件末尾）。
let BROWSER = null;
let PAGE = null;

async function main() {
const browser = await puppeteer.connect({ browserURL: process.env.BROWSER_URL || 'http://127.0.0.1:9444' });
BROWSER = browser;
const page = await browser.newPage();
PAGE = page;
await page.setViewport({ width: 1080, height: 900 });
// 只放行 GET；写请求一律挡下并回假 ok —— 体检绝不能改到真实数据
await page.evaluateOnNewDocument(() => {
  const real = window.fetch.bind(window);
  window.__blocked = [];
  window.__wbody = [];      // 连请求体一起记：光知道「打了哪个口」证明不了字段名对不对
  window.__fakeparents = null;
  // 「并入 / 拆出」（⑩）要**看归并之后长什么样**，光回一个 {ok:true} 是看不到的 ——
  // 所以这一个口照服务端 do_POST 的形状回一份（字段名、语义一样：parent 空串 = 拆出来），
  // 并且**跟真服务端一样把它记住**：/api/board 每 10 秒往回拉一次，真服务端那时已经落盘了，
  // 拉回来的是同一份；假响应要是不记住，刚并完下一秒就被那份空的盖回去（踩过）。
  // 仍然是纯内存的假响应，一个字节都没落到她的 app_parents.json 上。
  const fakeParent = (init) => {
    let body = {};
    try { body = JSON.parse((init && init.body) || '{}'); } catch (e) { body = {}; }
    // ⚠️ 底本必须先有真数据再合成（2026-10-09 踩到，一次假红三连）：
    // 页面第一次 /api/board 要等 10 秒轮询才来，在那之前 window.__fakeparents 还是 null；
    // 用 null 当底本 = 一张**空**映射表，假响应一回，页面就把整张归并表覆盖成只剩这一条 →
    // 顶层行数从 171 暴涨到 189（= 全部公司都不合并了）、概览「已归并 18→2」。
    // 所以底本按【已种上的 → 页面当前真正在用的 APP_PARENT → 空】取，跟轮询相位脱钩。
    const base = window.__fakeparents
      || (typeof APP_PARENT !== 'undefined' ? APP_PARENT : null) || {};
    const cur = Object.assign({}, base);
    const child = String(body.child || ''), parent = String(body.parent || '');
    if (parent && child !== parent) cur[child] = parent; else delete cur[child];
    window.__fakeparents = cur;
    return new Response(JSON.stringify({ ok: true, child: child,
                                         parent: cur[child] || null, app_parents: cur }),
                        { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  const jr = (o, status) => new Response(JSON.stringify(o),
    { status: status || 200, headers: { 'Content-Type': 'application/json' } });
  window.fetch = async (...a) => {
    const u = String((a[0] && a[0].url) || a[0]);
    const m = ((a[1] && a[1].method) || 'GET').toUpperCase();
    if (m !== 'GET') {
      window.__blocked.push(m + ' ' + u);
      window.__wbody.push({ m: m, u: u, body: String((a[1] && a[1].body) || '') });
      if (/\/api\/app-parent$/.test(u)) return fakeParent(a[1]);
      // 后台同步那一下：体检本来就不许真去拉数据，如实回「拉不动」，页面就收手了
      // （回 {ok:true} 的话它会当成"同步中"，每 1.2 秒 pull 一次、连拉 8 次，白等 10 秒）
      if (/\/api\/apps-sync$/.test(u)) return jr({ ok: false, error: '体检不触发同步' });
      return jr({ ok: true });
    }
    if (/\/api\/board$/.test(u)) {
      const res = await real(...a);
      try {
        const d = await res.json();
        if (window.__fakeparents === null) window.__fakeparents = d.app_parents || {};
        d.app_parents = window.__fakeparents;
        return jr(d, res.status);
      } catch (e) { return res; }
    }
    return real(...a);
  };
});
const errs = [];
page.on('pageerror', (e) => errs.push(String(e).slice(0, 200)));
page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 160)); });
// DEBUG_NAV=1 时把每一次主框架导航打到 stderr：偶发的「detached Frame」崩
// （跑着跑着 Execution context 没了）多半是页面在体检背后自己又导航了一次，
// 没有这个时间线就只能靠猜。平时不开，零成本。
if (process.env.DEBUG_NAV) {
  const T0 = Date.now();
  page.on('framenavigated', (f) => {
    if (f === page.mainFrame()) console.error('[nav %dms] %s', Date.now() - T0, f.url());
  });
}

// 清掉筛选记忆再进页面：不带上一轮的残留条件，结果才可复现
// 顺便把上一页记下的写请求收进累计表 —— `evaluateOnNewDocument` 每次导航都会重置
// window.__blocked/__wbody，不收的话「全程」那两条断言其实只看得到**最后一次**页面加载，
// 是个假的安全感（2026-10-04 修）。
const ALL_BLOCKED = [];
const ALL_WBODY = [];
async function drainWrites() {
  try {
    const r = await page.evaluate(() => ({ b: window.__blocked || [], w: window.__wbody || [] }));
    ALL_BLOCKED.push(...r.b);
    ALL_WBODY.push(...r.w);
  } catch (e) { /* 还没导航过 / 页面在跳，忽略 */ }
}
async function fresh(hash, clearKey) {
  await drainWrites();
  await page.goto(BASE + '/#' + hash, { waitUntil: 'domcontentloaded', timeout: 30000 });
  // clearKey 可以是数组：投递进度页除了搜索词，还有「哪几家公司被展开了」要一起清，
  // 不然上一轮留下的展开状态会让折叠的断言时好时坏
  if (clearKey) await page.evaluate((k) => {
    [].concat(k).forEach((x) => localStorage.removeItem(x));
  }, clearKey);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForFunction('document.querySelectorAll("#oppTb tr, #appTb tr").length > 0', { timeout: 25000 })
    .catch(() => {});
  await sleep(900);
  // 等执行环境真的安静下来再放行：页面偶尔在 load 完之后还会自己动一下
  // （轮询带回的东西触发的自刷新之类），紧跟在 fresh() 后面的第一个 evaluate
  // 就会撞上「Attempted to use detached Frame」。这里要求**连续两次**探测都成功
  // （间隔 400ms）—— 光成功一次不算数，下一次可能正好又赶上导航。
  // 2026-10-08 实测崩过两次，都崩在 fresh() 之后的第一个 evaluate 上。
  let quietStreak = 0;
  for (let i = 0; i < 12 && quietStreak < 2; i++) {
    try { await page.evaluate(() => 1); quietStreak++; }
    catch (e) {
      if (!/detached Frame|Execution context|Target closed|Cannot find context/i.test(String(e))) throw e;
      quietStreak = 0;
    }
    await sleep(400);
  }
}

const MEASURE = () => {
  const bar = document.querySelector('#pane-opps .bar');
  const nodes = [...bar.querySelectorAll('select,input,button,span')]
    .filter((e) => e.getBoundingClientRect().width > 0);
  const ov = [];
  for (let i = 0; i < nodes.length; i++)
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i], b = nodes[j];
      if (a.contains(b) || b.contains(a)) continue;
      const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
      const ox = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      const oy = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (ox > 4 && oy > 4) ov.push(`${a.tagName}✕${b.tagName} ${Math.round(ox)}×${Math.round(oy)}`);
    }
  let squeezed = 0;
  bar.querySelectorAll('.qrows').forEach((g) => { if (g.scrollWidth > g.clientWidth + 1) squeezed++; });
  return {
    ov, squeezed,
    sw: document.documentElement.scrollWidth, vw: window.innerWidth,
    count: document.getElementById('oppCount').textContent,
    kpiCur: (document.querySelector('#oppKpi .kpi-card[data-q="cur"] b') || {}).textContent,
  };
};

// 铺一屏「挂满条件」的样子 —— 空着测不出压不压
const FILL = () => {
  const s = (ms) => new Promise((r) => setTimeout(r, ms));
  const setRow = (box, add, i, f, v) => {
    let n = 0;
    while (document.querySelectorAll('#' + box + ' .qrow').length <= i && n++ < 12)
      document.getElementById(add).click();
    const r = document.querySelectorAll('#' + box + ' .qrow')[i];
    const sel = r.querySelector('select');
    sel.value = f; sel.dispatchEvent(new Event('change', { bubbles: true }));
    const inp = r.querySelector('input');
    inp.value = v; inp.dispatchEvent(new Event('input', { bubbles: true }));
  };
  setRow('incRows', 'incAdd', 0, 'batch', '秋招');
  setRow('incRows', 'incAdd', 1, 'job', '产品');
  setRow('excRows', 'excAdd', 0, 'all', '算法');
  setRow('excRows', 'excAdd', 1, 'all', '机器人');
  setRow('excRows', 'excAdd', 2, 'all', '爆');
  return s(900);
};

console.log('\n########## 看板体检 ##########');

// ---------- ① 计数口径 ----------
console.log('\n① 计数口径');
await fresh('opps', 'at.opps.q.v1');
let m = await page.evaluate(MEASURE);
// 计数现在报两个口径：**家数**（表里一行一行数得出来的）和**条数**（源表记录数）。
// KPI「筛出公司」那个大数字就是家数，跟标签第一个数必须一致。
check('默认态：右下角计数与 KPI「筛出公司」卡是同一个数',
  m.count.startsWith('筛出 ' + m.kpiCur + ' 家（'), `标签「${m.count}」卡 ${m.kpiCur}`);
check('默认态如实标注有筛选在生效（默认开着「还没截止」）',
  m.count.startsWith('筛出 '), `「${m.count}」`);
await page.evaluate(() => document.getElementById('oppOpen').click());
// 等这一格真的重渲染（标签变了），别写死某个条数 —— 那是使用者自己表里的数字，
// 而且过一天就变了，写死等于给自己埋一个迟早失败的断言。
await page.waitForFunction((p0) => document.getElementById('oppCount').textContent !== p0,
  { timeout: 8000 }, m.count).catch(() => {});
await sleep(300);
m = await page.evaluate(MEASURE);
check('关掉「还没截止」→ 恢复「共 M 家公司 · K 条岗位」，与卡一致',
  m.count.trim().startsWith('共 ' + m.kpiCur + ' 家公司 · ') && / · \d+ 条岗位$/.test(m.count.trim()),
  `「${m.count}」卡 ${m.kpiCur}`);

// ---------- ② 各宽度下筛选栏会不会自己压自己 ----------
console.log('\n② 筛选栏布局（挂满条件后逐档宽度量）');
await fresh('opps', 'at.opps.q.v1');
await page.evaluate(FILL);
for (const w of WIDTHS) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(350);
  const r = await page.evaluate(MEASURE);
  check(`${w}px：无重叠 / 无内容挤压 / 无横向溢出`,
    r.ov.length === 0 && r.squeezed === 0 && r.sw <= r.vw,
    `重叠 ${r.ov.length} 处 ${r.ov.slice(0, 3).join('; ')}｜挤压 ${r.squeezed} ｜宽 ${r.sw}/${r.vw}`);
}
await page.setViewport({ width: FOCUS, height: 900 });
await sleep(400);

// ---------- ③ 主要交互 ----------
console.log('\n③ 主要交互');
await fresh('opps', 'at.opps.q.v1');

for (const [q, label] of [['y27', '2027 届能投'], ['exam', '含免笔试'], ['mine', '我投过的公司'], ['fresh', '近 7 天新开']]) {
  await fresh('opps', 'at.opps.q.v1');
  const before = await page.evaluate(MEASURE);
  await page.waitForFunction((qq) => !!document.querySelector(`#oppKpi .kpi-card[data-q="${qq}"]`), { timeout: 8000 }, q);
  await page.evaluate((qq) => document.querySelector(`#oppKpi .kpi-card[data-q="${qq}"]`).click(), q);
  await sleep(1400);
  const on = await page.evaluate((qq) => document.querySelector(`#oppKpi .kpi-card[data-q="${qq}"]`).classList.contains('on'), q);
  const after = await page.evaluate(MEASURE);
  check(`KPI「${label}」点一下 → 点亮且结果变化`, on && after.count !== before.count, `on=${on}「${before.count}」→「${after.count}」`);
}
await fresh('opps', 'at.opps.q.v1');
for (const [id, label] of [['oppSortCo', '按公司名'], ['oppSortInd', '按行业'], ['oppSortDl', '按截止时间']]) {
  await page.evaluate((i) => document.getElementById(i).click(), id);
  const on = await page.waitForFunction((i) => document.getElementById(i).classList.contains('on'),
    { timeout: 8000 }, id).then(() => true).catch(() => false);
  check(`排序「${label}」按钮点亮`, on);
}
await fresh('opps', 'at.opps.q.v1');
const seq = [];
for (let i = 0; i < 5; i++) {
  // 每换一档都要重拉一次列表，标签是渲染时才写的 —— 死等固定毫秒会读到上一档，
  // 所以等到标签真的变了再往下走
  const prev = await page.evaluate(() => document.getElementById('oppNew').textContent.trim());
  await page.evaluate(() => document.getElementById('oppNew').click());
  await page.waitForFunction((p0) => document.getElementById('oppNew').textContent.trim() !== p0,
    { timeout: 9000 }, prev).catch(() => {});
  await sleep(250);
  seq.push(await page.evaluate(() => ({
    lab: document.getElementById('oppNew').textContent.trim(),
    up: document.getElementById('oppSortUp').classList.contains('on'),
    tags: document.querySelectorAll('#oppTb .fresh').length,
    count: document.getElementById('oppCount').textContent,
  })));
}
check('「最近新开」档位能循环', new Set(seq.map((x) => x.lab)).size >= 4, seq.map((x) => x.lab).join(' → '));
check('开着时排序自动切「按更新时间」', seq.some((x) => x.up), seq.map((x) => x.up).join('/'));
// 「今天 / N 天前」那小标只在**源表里真有 3 天内的更新**时才画 —— 她那张源表国庆期间没人维护，
// 最新一条停在 09-30，今天是 10-04，那 0 个标就是对的。所以按真实数据判，
// 别写成「必须有标」，否则每次假期过几天都会假红一条。
const oppNew3 = await page.evaluate(async () => {
  const r = await fetch('/api/opps?limit=1');
  const d = await r.json().catch(() => ({}));
  return (d.stats && d.stats.new3) || 0;
});
check('「今天 / N 天前」小标跟源表真实新旧一致（近 3 天有新岗位时才该出现）',
  oppNew3 > 0 ? seq.some((x) => x.tags > 0) : seq.every((x) => x.tags === 0),
  '源表近 3 天新增=' + oppNew3 + '，各档小标数=' + seq.map((x) => x.tags).join('/'));
await fresh('opps', 'at.opps.q.v1');
const rows = await page.evaluate(async () => {
  const n = () => document.querySelectorAll('#incRows .qrow').length;
  const onlyOneNoX = getComputedStyle(document.querySelector('#incRows .qrow .qdel')).display === 'none';
  document.getElementById('incAdd').click();
  await new Promise((r) => setTimeout(r, 250));
  const two = n();
  const xShown = getComputedStyle(document.querySelector('#incRows .qrow .qdel')).display !== 'none';
  document.querySelector('#incRows .qrow .qdel').click();
  await new Promise((r) => setTimeout(r, 700));
  return { one: n(), onlyOneNoX, two, xShown };
});
check('条件行能加能删，且只剩一条时不给删（不会删成空框）',
  rows.onlyOneNoX && rows.two === 2 && rows.xShown && rows.one === 1, JSON.stringify(rows));
const facet = await page.evaluate(async () => {
  const c = () => document.getElementById('oppCount').textContent;
  const before = c();
  document.querySelector('#oppByInd .fbar').click();
  await new Promise((r) => setTimeout(r, 1200));
  const picked = c();
  const on = !!document.querySelector('#oppByInd .fbar.on');
  document.querySelector('#oppByInd .fbar').click();
  await new Promise((r) => setTimeout(r, 1200));
  return { before, picked, on, back: c() };
});
check('分布面板点一下筛选、再点取消',
  facet.on && facet.picked !== facet.before && facet.back === facet.before, JSON.stringify(facet));

console.log('\n④ 投递进度页');
await fresh('apps', ['at.apps.q.v1', 'at.apps.open.v1']);
const note = await page.evaluate(async () => {
  const el = document.querySelector('#appTb .noteview') || document.querySelector('#appTb .noteadd');
  if (!el) return { ok: false, why: '没有备注入口' };
  el.click();
  for (let i = 0; i < 12 && !document.querySelector('#appTb input, #appTb textarea'); i++)
    await new Promise((r) => setTimeout(r, 200));
  const inp = document.querySelector('#appTb input, #appTb textarea');
  if (!inp) return { ok: false, why: '点了没变输入框' };
  inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await new Promise((r) => setTimeout(r, 400));
  return { ok: true };
});
check('备注点一下变输入框、Esc 能撤（写请求已拦）', note.ok, JSON.stringify(note));

// 整行都能改（v0.23.0）：公司名 / 岗位名 / 投递时间 / 链接 点一下都进编辑态。
// 写请求被拦成假 ok，所以这里只能验「进没进编辑态、带没带原值、发出去的字段名对不对」——
// 真落盘那一段交给自检里的 update_app 断言。
const cellEdit = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { steps: [], writes: [] };
  const before = (window.__wbody || []).length;
  const plain = (sel) => document.querySelector('#appTb tr:not(.grp) ' + sel);
  const dated = () => [...document.querySelectorAll('#appTb tr:not(.grp) td.dl .cellv')]
    .find((e) => /^\d{4}-\d{2}-\d{2}/.test(e.dataset.v || '')) || null;
  // 注意这里存的是**取值函数**，不是元素本身：每改一格页面就重铺一次 DOM，
  // 早先存下来的元素会变成游离节点，点它等于点空气（第一版就是这么假红的）。
  const plan = [
    ['company', () => plain('.c-name .cellv'), '星桥科技（改）'],
    ['position', () => plain('.c-pos .cellv'), '内容运营（改）'],
    ['when', () => dated(), '2026-09-01 09:30'],
  ];
  for (const [f, get, nv] of plan) {
    const el = get();
    if (!el) { out.steps.push({ f: f, why: 'no-el' }); continue; }
    const v0 = el.dataset.v;
    el.click();
    for (let i = 0; i < 15 && !document.querySelector('#appTb input.cellin'); i++) await wait(120);
    const inp = document.querySelector('#appTb input.cellin');
    if (!inp) { out.steps.push({ f: f, why: 'no-input' }); continue; }
    out.steps.push({ f: f, type: inp.type, v0: v0, shown: inp.value });
    inp.value = (inp.type === 'datetime-local') ? '2026-09-01T09:30' : nv;
    inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await wait(700);
  }
  // 链接那格平时是「打开」，下面一行淡淡的「改链接」（v0.23.1 —— 两个控件挤不进 72px 的列），点它也要能改
  const lk = document.querySelector('#appTb tr:not(.grp) .lnkedit');
  out.link = { exists: !!lk };
  if (lk) {
    lk.click();
    for (let i = 0; i < 15 && !document.querySelector('#appTb input.cellin'); i++) await wait(120);
    const inp = document.querySelector('#appTb input.cellin');
    out.link.editable = !!inp;
    if (inp) {
      out.link.value = inp.value;
      inp.value = 'https://example.com/job/1';
      inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      await wait(700);
    }
  }
  out.leftOpen = !!document.querySelector('#appTb input.cellin');
  out.writes = (window.__wbody || []).slice(before)
    .filter((w) => /\/api\/status$/.test(w.u)).map((w) => w.body);
  return out;
});
const ceOf = (f) => cellEdit.steps.filter((s) => s.f === f)[0] || {};
check('整行可改：公司名 / 岗位名 / 投递时间点一下都进编辑态，且带出原值',
  ['company', 'position', 'when'].every((f) => ceOf(f).type) &&
  ceOf('company').shown === ceOf('company').v0 &&
  ceOf('position').shown === ceOf('position').v0 &&
  ceOf('when').type === 'datetime-local' &&
  ceOf('when').shown.slice(0, 10) === ceOf('when').v0.slice(0, 10) &&
  !cellEdit.leftOpen,
  JSON.stringify(cellEdit.steps));
check('整行可改：链接那格点「改链接」也能改（原来只能跳出去看，改不了）',
  cellEdit.link.exists && cellEdit.link.editable, JSON.stringify(cellEdit.link));
check('整行可改：改完发的是 POST /api/status，四种字段的参数名都对（没碰别的接口）',
  cellEdit.writes.length === 4 &&
  cellEdit.writes.every((b) => b.indexOf('"id"') >= 0) &&
  ['company', 'position', 'applied_at', 'url']
    .every((k) => cellEdit.writes.some((b) => b.indexOf('"' + k + '"') >= 0)),
  JSON.stringify(cellEdit.writes).slice(0, 260));
let statusWritesExpected = cellEdit.writes.length;

// 编辑态必须是「浮在格子上」的，一个像素都不许动表格布局（v0.23.1）。
// 这张表是 `table{width:100%}` 的 auto 布局，总宽恒等于容器宽 —— 所以「总宽没变」什么都证明不了，
// 列宽会在列与列之间互相挪。要量三样：**逐列宽、总宽、行高**。
// 两条历史：① `.cellv` 用 `word-break:break-word`（= `overflow-wrap:anywhere`）会把「岗位」列
// 从 114px 压到 84px，岗位名一字一行；② 编辑框 `width:100%`（备注那格还带 `min-width:130px`）
// 会被 auto 布局当内在尺寸算，链接列 76→119px、备注一点编辑整张表撑到 1131px。
// 这里只用 Esc 退出，所以**一个写请求都不会发**（上面的计数断言照旧成立）。
await page.setViewport({ width: 1080, height: 900 });
const editLayout = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const tb = document.querySelector('#appTb');
  const table = tb.closest('table');
  const ths = [...table.querySelectorAll('thead th')];
  const total = () => Math.round(table.getBoundingClientRect().width);
  const rowH = () => [...tb.children].filter((r) => r.tagName === 'TR')
    .map((r) => Math.round(r.getBoundingClientRect().height));
  const snap = () => ({ cols: ths.map((t) => Math.round(t.getBoundingClientRect().width)),
                        h: rowH(), total: total(),
                        over: Math.round(total() - table.parentElement.clientWidth) });
  let last = '';
  for (let i = 0; i < 12; i++) { await wait(220); const s = JSON.stringify(snap()); if (s === last) break; last = s; }
  const base = snap();
  const out = { base: base, cases: [] };
  // ⚠️ 量完必须确认手里那个输入框还在文档里：页面自己定时 `pull()` 重铺 DOM 的话，
  // 拿到的 rect / computed style 全是空值或旧值，「列宽没变」就是个**假绿**（第一版就栽在这，
  // 链接 / 备注两格报 pos 空、Δ全 0）。所以每次量完查 `isConnected`，掉线就整格重来。
  const probe = async (label, sel) => {
    for (let attempt = 0; attempt < 3; attempt++) {
      const el = tb.querySelector('tr:not(.grp) ' + sel);
      if (!el) { out.cases.push({ label, why: 'no-el' }); return; }
      el.click();
      for (let i = 0; i < 15 && !tb.querySelector('input.cellin'); i++) await wait(120);
      const inp = tb.querySelector('input.cellin');
      if (!inp) { out.cases.push({ label, why: 'no-input' }); return; }
      // 绝对定位的是**浮层**、不是输入框（v0.24.2 起定位从 input 挪到 .cellpop 上，
      // 因为时间那格的浮层里还排着「今天 / 昨天 / 前天」三个按钮）
      const pop = tb.querySelector('td.editing .cellpop') || inp;
      const pos = getComputedStyle(pop).position;
      const now = snap();                       // 读 rect 会强制布局，不用额外等
      if (!inp.isConnected) { await wait(500); continue; }   // 页面刚好重铺了，重来
      out.cases.push({ label, pos, tries: attempt + 1,
                       dCols: now.cols.map((v, i) => v - base.cols[i]).filter((v) => v !== 0),
                       dTotal: now.total - base.total, over: now.over,
                       hChanged: now.h.filter((v, i) => Math.abs(v - base.h[i]) > 1).length });
      inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await wait(700);
      return;
    }
    out.cases.push({ label, why: 'stale（三次都被页面重铺打断，没量到）' });
  };
  await probe('company', '.c-name .cellv');
  await probe('position', '.c-pos .cellv');
  await probe('when', 'td.dl .cellv');
  await probe('link', '.lnkedit');
  await probe('note', '.noteview');
  return out;
});
check('整行可改：编辑浮层是浮在格子上面的（绝对定位），不是把格子内容换成输入框',
  editLayout.cases.length === 5 && editLayout.cases.every((c) => c.pos === 'absolute'),
  JSON.stringify(editLayout.cases.map((c) => [c.label, c.pos || c.why])));
check('整行可改：进编辑态一格都不动表格布局（五种格子逐列量：列宽 / 总宽 / 行高全不变、没有横向溢出）',
  editLayout.cases.length === 5 &&
  editLayout.cases.every((c) => !c.why && c.dCols.length === 0 && c.dTotal === 0 &&
                              c.over <= 0 && c.hChanged === 0),
  JSON.stringify(editLayout.cases));

// 投递时间那格怎么改（她 2026-10-05 截图框着「＋时间」问「改时间方式能不能方便一点，
// 还要我一个个手打。。」）。老写法：值是空的就退化成纯文本框，只能从零敲一个日期。
// 现在：空值预填今天 + 一排「今天 / 昨天 / 前天」点一下直接存。
const whenQuick = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const p2 = (n) => (n < 10 ? '0' : '') + n;
  const t = new Date();
  const today = t.getFullYear() + '-' + p2(t.getMonth() + 1) + '-' + p2(t.getDate());
  const cells = () => [...document.querySelectorAll('#appTb tr:not(.grp) td.dl .cellv')];
  const el = cells().find((e) => !String(e.dataset.v || '').trim()) || cells()[0] || null;
  if (!el) return { why: '没有投递时间格' };
  const out = { wasEmpty: !String(el.dataset.v || '').trim(), today: today };
  const before = (window.__wbody || []).length;
  el.click();
  for (let i = 0; i < 15 && !document.querySelector('#appTb .cellpop'); i++) await wait(120);
  const pop = document.querySelector('#appTb .cellpop');
  if (!pop) return { why: '没进编辑态' };
  const inp = pop.querySelector('input.cellin');
  const qs = [...pop.querySelectorAll('.cellquick button')];
  out.btns = qs.map((b) => b.textContent.trim());
  out.inpType = inp ? inp.type : '';
  out.seed = inp ? inp.value : '';
  out.seedIsToday = String(out.seed).slice(0, 10) === today;
  const b = qs.find((x) => x.textContent.trim() === '今天');
  if (b) {
    b.click();
    await wait(1000);
    out.writes = (window.__wbody || []).slice(before)
      .filter((x) => /\/api\/status$/.test(x.u)).map((x) => x.body);
  }
  out.leftOpen = !!document.querySelector('#appTb .cellpop');
  return out;
});
check('投递时间那格：浮层里有「今天 / 昨天 / 前天」三个快捷按钮，输入框是原生日期时间选择器',
  !!whenQuick.btns && whenQuick.btns.join('/') === '今天/昨天/前天' &&
  whenQuick.inpType === 'datetime-local',
  JSON.stringify(whenQuick));
check('投递时间空着的那格，点开就预填今天（原来只能从零手打；这条正是她截图框的那格）',
  whenQuick.wasEmpty === false || whenQuick.seedIsToday === true,
  JSON.stringify({ wasEmpty: whenQuick.wasEmpty, seed: whenQuick.seed, today: whenQuick.today }));
check('点「今天」直接存：只发一次 /api/status，体里只有这一条的 id + applied_at，值就是今天，浮层自动收起',
  !!whenQuick.writes && whenQuick.writes.length === 1 &&
  whenQuick.writes[0].indexOf('"applied_at"') >= 0 &&
  whenQuick.writes[0].indexOf(whenQuick.today) >= 0 &&
  whenQuick.writes[0].indexOf('"status"') < 0 &&   whenQuick.leftOpen === false,
  JSON.stringify({ w: whenQuick.writes, leftOpen: whenQuick.leftOpen }));
// 上面点「今天」那一下是**故意**写的（跟第 4 类那几下改格子一样），记账时要算进去
statusWritesExpected += (whenQuick.writes || []).length;

// 同公司多岗位：折成一组、点开能看岗位、计数按公司数。她这句需求的原话是
// 「同一个公司投递多个岗位的话，在同一个公司下面增加子记录归纳到一块，
//   总投递按照公司数计算」——以后改这块先看这两条有没有红。
const fold = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const rows = () => [...document.querySelectorAll('#appTb tr')];
  const grp = () => rows().filter((r) => r.classList.contains('grp'));
  const sub = () => rows().filter((r) => r.classList.contains('sub'));
  const btn = document.getElementById('appOpenAll');
  const label = () => btn.textContent.trim();
  // 上一轮可能留了「全部展开」，先归零，基线才是收起的
  if (label() === '收起全部') { btn.click(); await wait(400); }
  const closed = { grp: grp().length, sub: sub().length, hidden: btn.hidden, label: label() };
  // 挑一家岗位最多的点开（岗位数写在徽标里）
  // ⚠️ 只挑**没归并过**的组：归并过的主行徽标前面是「N 家子公司」，parseInt 会拿子公司数
  // 当岗位数，折叠对不对就量歪了（归并那套由 ⑩ 单独量）。
  const plain = grp().filter((r) => String((r.querySelector('.gn') || {}).textContent || '').indexOf('家子公司') < 0);
  const pool = plain.length ? plain : grp();
  const most = pool.slice().sort((a, b) =>
    parseInt(b.querySelector('.gn').textContent, 10) - parseInt(a.querySelector('.gn').textContent, 10))[0];
  const n = most ? parseInt(most.querySelector('.gn').textContent, 10) : 0;
  // 手型要在点击**之前**读 —— 点完整行会 renderApps() 重建 DOM，旧引用是 detached 的，
  // 对它 getComputedStyle 拿到的是空字符串（踩过一次，别改回去）
  const cursor = most ? getComputedStyle(most).cursor : '';
  // 点公司名那一格（**不是**那个小三角）—— 她的要求是「点这一行就能展开」
  if (most) most.children[0].click();
  await wait(450);
  const one = { sub: sub().length, open: grp().some((r) => r.classList.contains('open')),
                arw: (() => {
                  const o = grp().find((r) => r.classList.contains('open'));
                  return o ? getComputedStyle(o.querySelector('.arw')).transform : '';
                })(),
                cursor: cursor };
  // 再点「投递时间」那一格，验证是整行都能点、不是只有公司列
  const op = grp().find((r) => r.classList.contains('open'));
  if (op) op.children[3].click();
  await wait(450);
  const whole = { sub: sub().length };
  // 点小三角也要正好切一次（绑在 tr 上会冒泡，别处再绑一次就会触发两下、看着像没反应）
  if (most) most.querySelector('.gtoggle').click();
  await wait(450);
  const byArrow = { sub: sub().length };
  btn.click();
  await wait(450);
  const all = { sub: sub().length, label: label() };
  btn.click();
  await wait(450);
  const back = { sub: sub().length, label: label() };
  return { closed, n, one, whole, byArrow, all, back,
           kpiTotal: document.querySelector('#appKpi .kpi-card b').textContent.trim(),
           tab: document.getElementById('tabApps').textContent.trim(),
           countText: document.getElementById('appCount').textContent.trim(),
           overview: (() => { const o = document.getElementById('appOverview');
                              return (o.textContent + ' ' + (o.title || '')).replace(/\s+/g, ' '); })() };
});
check('投递进度页同公司多岗位折成一组，点一下展开、按钮能一键全开全收',
  fold.closed.grp > 0 && !fold.closed.hidden && fold.closed.sub === 0 &&
  fold.one.sub === fold.n && fold.one.open && fold.one.arw !== 'none' &&
  fold.all.sub > fold.one.sub && fold.all.label === '收起全部' &&
  fold.back.sub === 0 && fold.back.label === '展开全部',
  JSON.stringify([fold.closed, fold.n, fold.one, fold.all, fold.back]));
// 她的原话：「展开的方式也太不方便了，要去点这个小三角才能展开，需要点这一行就能展开」
// v0.27.0 起「能拖的行」光标是 grab（张手，见 ⑩b）—— 那也是个手型，照样表示「这一行能点」，
// 点击行为本身一点没变（下面那两个计数就是证据：点时间格不展开、点三角才展开）。
check('投递进度页整行都能点开（不只那个小三角），点它也只会切一次',
  ['pointer', 'grab'].indexOf(fold.one.cursor) >= 0 &&
  fold.whole.sub === 0 && fold.byArrow.sub === fold.n,
  JSON.stringify({ cursor: fold.one.cursor, 点时间格: fold.whole.sub, 点三角: fold.byArrow.sub, n: fold.n }));
check('投递进度页「总投递」按公司数算，跟概览 / 标签页 / 右下角计数一个口径',
  fold.kpiTotal === fold.tab && fold.countText.indexOf('共 ' + fold.kpiTotal + ' 家') >= 0 &&
  fold.overview.indexOf('涉及公司') >= 0 && fold.overview.indexOf(fold.kpiTotal + ' 家') >= 0,
  'KPI=' + fold.kpiTotal + ' tab=' + fold.tab + ' 计数=' + fold.countText);

// ---------- ⑤ 配色切换（自动 / 浅色 / 深色） ----------
// 三件事必须同时在：属性对了、颜色真的跟着变、选择被记住。
// 只断言属性等于 dark 是不够的 —— 属性写对了但某个块还铺着白底，肉眼看就是花的。
console.log('\n⑤ 配色切换');
// 把颜色算成「和」，深底 / 浅底、深字 / 浅字一眼可分（不引额外的颜色库）
const THEME_PROBE = () => {
  const sum = (s) => { const m = s.match(/\d+/g) || []; return (+m[0] || 0) + (+m[1] || 0) + (+m[2] || 0); };
  const cs = getComputedStyle(document.body);
  const p = document.getElementById('addToggle');       // 实心主按钮（强调色底 + 字色）
  return {
    mode: document.documentElement.getAttribute('data-theme-mode'),
    attr: document.documentElement.getAttribute('data-theme'),
    bg: sum(cs.backgroundColor), fg: sum(cs.color),
    on: [...document.querySelectorAll('#themeBar button')].filter((b) => b.classList.contains('on'))
          .map((b) => b.dataset.thm),
    btn: p ? { bg: sum(getComputedStyle(p).backgroundColor), fg: sum(getComputedStyle(p).color) } : null,
    stored: (() => { try { return localStorage.getItem('at.theme.v1') || ''; } catch (e) { return 'x'; } })(),
  };
};
await page.evaluate(() => { try { localStorage.removeItem('at.theme.v1'); } catch (e) {} });
await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
await fresh('apps');
let t = await page.evaluate(THEME_PROBE);
check('默认「自动」：不写死主题，交给系统偏好（系统浅色就是浅色）',
  t.mode === 'auto' && t.attr === null && t.bg > 400 && t.on.join() === 'auto', JSON.stringify(t));

await page.evaluate(() => document.querySelector('#themeBar button[data-thm="dark"]').click());
await sleep(250);
t = await page.evaluate(THEME_PROBE);
// 页面每 10 秒自己 `pull()` 重铺一次；正好撞上时，主按钮是个刚换上的新节点，
// 背景还走在 `button{transition:background .12s}` 的半路上 —— `getComputedStyle` 给的是插值。
// 2026-10-05 就这么假红过一次：**同一次采样**里 body 已经变深、主按钮还是浅色底（369）。
// 判据一个字都不改，只是在"看着像还在过渡"时再采一次。
if (t.btn && t.bg < 200 && t.btn.bg > 400) {
  await sleep(500);
  t = await page.evaluate(THEME_PROBE);
}
check('切「深色」→ 底色真变深、字跟着变浅、按钮点亮、选择被记住',
  t.mode === 'dark' && t.attr === 'dark' && t.bg < 200 && t.fg > 400 &&
  t.on.join() === 'dark' && t.stored.indexOf('"dark"') >= 0, JSON.stringify(t));
// 深色下强调色是浅蓝，白字压上去对比度不够（WCAG AA）—— 这条专门盯着主按钮
check('深色下实心主按钮换成深字（浅蓝底配白字看不清）',
  t.btn && t.btn.bg > 400 && t.btn.fg < 200, JSON.stringify(t.btn));
// 这条是「能切」的核心：系统偏好是深色，手动选浅色也必须压过去
await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
await page.evaluate(() => document.querySelector('#themeBar button[data-thm="light"]').click());
await sleep(250);
t = await page.evaluate(THEME_PROBE);
check('系统是深色时手动选「浅色」→ 照样浅色（人工选择压过系统偏好）',
  t.mode === 'light' && t.attr === 'light' && t.bg > 400, JSON.stringify(t));
await page.evaluate(() => document.querySelector('#themeBar button[data-thm="auto"]').click());
await sleep(250);
t = await page.evaluate(THEME_PROBE);
check('选回「自动」→ 重新跟随系统（此刻系统是深色，页面就该是深的）',
  t.mode === 'auto' && t.attr === null && t.bg < 200, JSON.stringify(t));

await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
await page.evaluate(() => document.querySelector('#themeBar button[data-thm="dark"]').click());
await sleep(200);
// 刷新前先看一眼：属性必须在文档一开始解析（<head>）时就写上，
// 放到 body 末尾才写的话，刷新会先闪一屏浅色
await page.goto(BASE + '/#apps', { waitUntil: 'domcontentloaded', timeout: 30000 });
const early = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
await sleep(700);
t = await page.evaluate(THEME_PROBE);
check('刷新后还是深色（记在浏览器里），且是在首屏绘制前就定下来的（不闪一屏浅色）',
  early === 'dark' && t.mode === 'dark' && t.attr === 'dark' && t.bg < 200, '早期=' + early + ' ' + JSON.stringify(t));

// 配色切换挤在标签栏右端，窄窗口下别把标签栏撑破
for (const w of [1440, 1280, 1100, 1080, 1024, 980, 940]) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(300);
  const r = await page.evaluate(() => {
    const tabs = document.querySelector('.tabs'), bar = document.getElementById('themeBar');
    const rb = bar.getBoundingClientRect(), rt = tabs.getBoundingClientRect();
    return {
      squeezed: bar.scrollWidth - bar.clientWidth,
      inside: rb.right <= rt.right + 1 && rb.left >= rt.left - 1,
      sw: document.documentElement.scrollWidth, vw: window.innerWidth,
      gap: Math.round(rt.right - rb.right),
    };
  });
  check(`${w}px：配色切换待在标签栏右端，没被挤扁也没撑出横向滚动`,
    r.squeezed <= 1 && r.inside && r.sw <= r.vw, JSON.stringify(r));
}
await page.setViewport({ width: 1080, height: 900 });
// 收尾：把主题恢复成默认（自动），别把这个键留给下次体检
await page.evaluate(() => {
  try { localStorage.removeItem('at.theme.v1'); } catch (e) {}
  document.querySelector('#themeBar button[data-thm="auto"]').click();
});
await sleep(200);

// ---------- ⑥ 邮箱页的两个视图 + 投递进度 ----------
// 她的原话：「还是在这一页做两个可切换的看板吧。一个像之前一样专门显示各种面试测评截止时间，
// 另外一个拿来显示投递进度条（测评的不用显示）。」
// 进度条是**推断**出来的（读通知 + 投递状态），不是她手填的，所以「哪些公司该列进去」
// 「点一下能不能改」「切过去到底显示了没」都得真量一遍，不能看代码猜。
console.log('\n⑥ 邮箱页两个视图');
// 连视图记忆一起清掉 —— 不然上一轮停在哪一版，这一轮就默认在哪一版，断言会飘
// `at.ivf.v1` 是进度视图那排筛选项的记忆（v0.24.0 加的），不清的话上一轮选过哪一档会带进来
await fresh('notices', ['at.ntv.v1', 'at.ivf.v1']);
// 下面「只写 /api/interview」是按**本次页面加载**的增量算的。导航已经把账本清空了，
// 所以这里明着归零，别再去读上一页遗留的长度（那样子页面若自发过一次 POST，计数就会错位）。
await page.evaluate(() => { window.__blocked = []; window.__wbody = []; });
const blockedBefore = 0;

// 进来默认是「面试测评截止」那一版，先确认这一版是干净的
// （v0.21.0 那个「面试进度」列已经拿掉，别哪天又悄悄长回来）
const dl = await page.evaluate(() => {
  const r = document.querySelector('#pane-notices table thead tr');
  return {
    tab: document.documentElement.getAttribute('data-ntv'),
    cols: r ? [...r.querySelectorAll('th')].map((t) => t.textContent.trim()) : [],
    hasIvCol: !!document.querySelector('#ntTb td.c-iv'),
    dlH: document.querySelector('#ntDlView').getBoundingClientRect().height,
    ivH: document.querySelector('#ntIvView').getBoundingClientRect().height,
  };
});
check('默认进「面试测评截止」那一版，通知表回到 9 列、进度条那一列已经拿掉',
  dl.tab === 'dl' && dl.cols.length === 9 && dl.cols.indexOf('面试进度') < 0 && !dl.hasIvCol,
  JSON.stringify({ tab: dl.tab, cols: dl.cols }));
// 量渲染盒子的高度，不看 computed display —— 父级被 display:none 盖住时子元素自己的
// display 照样是它本来的值，那种判据会让「两块都藏着」一路绿灯
check('两个视图互斥：这一版看得见、另一版真的收起来了',
  dl.dlH > 0 && dl.ivH === 0, JSON.stringify({ dl: Math.round(dl.dlH), iv: Math.round(dl.ivH) }));

// 切到「投递进度」
await page.evaluate(() => document.getElementById('ntvIv').click());
await sleep(700);
const iv = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('#ivList .ivrow')];
  const items = rows.filter((r) => r.querySelector('.ivco b')).map((r) => {
    const ns = [...r.querySelectorAll('.ivn')];
    const cur = r.querySelector('.ivn.cur');
    const next = [...r.querySelectorAll('.ivn.next')];
    const oks = [...r.querySelectorAll('.ivn.ok')];
    const cs = next.length ? getComputedStyle(next[0]) : null;
    const cb = cur ? getComputedStyle(cur) : null;
    // 剩下几档**不能数 DOM** —— 灰块把没到的环节折成了一个。真正的清单写在它自己的 title
    // （「还没到：终面 / offer（…）」）里，从那儿读
    const ttl = next.length ? (next[0].getAttribute('title') || '') : '';
    const mm = ttl.match(/还没到：(.+?)（/);
    return {
      co: (r.querySelector('.ivco b') || {}).textContent.trim(),
      nodes: ns.map((n) => n.textContent.replace('?', '').trim()),
      cur: cur ? cur.dataset.node : '', ok: oks.length, next: next.length,
      dn: next[0] ? next[0].dataset.node : '',
      label: next.length ? next[0].textContent.replace('?', '') : '',
      restList: mm ? mm[1].split(' / ') : [],
      ask: cur ? cur.classList.contains('ask') : false,
      stopped: r.classList.contains('stopped'),
      okBg: oks.length ? getComputedStyle(oks[0]).backgroundColor : '',
      curBg: cb ? cb.backgroundColor : '',
      nextBg: cs ? cs.backgroundColor : '', nextBorder: cs ? cs.borderStyle : '',
      // 进度条末尾那个红「已挂」标（v0.24.1）。op 要一起量 —— 整行淡下去的规则
      // 写在父级 .ivbar 上的话，这个标会被一起淡掉（父级 opacity 是整体相乘）
      dead: (() => {
        const d = r.querySelector('.ivdead');
        if (!d) return null;
        const s = getComputedStyle(d);
        // 实际不透明度得把**祖先链**乘进去 —— 父级 .ivbar 上写一条 opacity 就能把它淡掉，
        // 而 getComputedStyle(d).opacity 照样报 '1'（那是它自己设的值，不是它看起来的）
        let op = 1, el = d;
        while (el && el !== document.body) { op *= parseFloat(getComputedStyle(el).opacity || '1'); el = el.parentElement; }
        return { txt: d.textContent.trim(), bd: s.backgroundColor, fg: s.color, op: Math.round(op * 1000) / 1000 };
      })(),
    };
  });
  const pb = document.getElementById('addToggle');
  // 把 --red 解析成 rgb 拿来对（别在断言里写死 #c8382f —— 深色主题里它换值）
  const rp = document.createElement('span');
  rp.style.color = 'var(--red)';
  document.body.appendChild(rp);
  const redRgb = getComputedStyle(rp).color;
  rp.remove();
  return {
    items: items,
    redRgb: redRgb,
    ivH: document.querySelector('#ntIvView').getBoundingClientRect().height,
    dlH: document.querySelector('#ntDlView').getBoundingClientRect().height,
    accentBg: pb ? getComputedStyle(pb).backgroundColor : '',
    hint: (document.getElementById('ntvHint') || {}).textContent || '',
    kpis: [...document.querySelectorAll('#ivKpi .kpi-card')].length,
    note: (document.getElementById('ivNote') || {}).textContent || '',
  };
});
check('切过去「投递进度」那一版真的显示出来、通知表收起来（两个视图各占一版）',
  iv.ivH > 0 && iv.dlH === 0, JSON.stringify({ iv: Math.round(iv.ivH), dl: Math.round(iv.dlH) }));
// 她说的「测评的不用显示」= 只停在测评阶段、还在往下推的那几家整条不列。
// v0.24.0 起有个例外：**止步的（挂了的）例外** —— 她 2026-10-05 说「示例涂料公司已经给我挂了」，
// 挂掉这件事必须在看板上看得见，不能再被这条「测评的不列」顺带藏掉。
check('只停在「测评」阶段、还在推进的公司整条不列；挂掉的（哪怕停在测评）照样列出来',
  iv.items.length > 0 && iv.items.every((x) => !/^测评/.test(x.cur) || x.stopped) &&
  iv.items.some((x) => x.stopped),
  JSON.stringify({ 在推的测评行: iv.items.filter((x) => /^测评/.test(x.cur) && !x.stopped).map((x) => x.co),
                   止步行: iv.items.filter((x) => x.stopped).map((x) => [x.co, x.cur]) }));
// 上一版是「按通知行画」，同一家有 3 条面试通知就会出现 3 根一模一样的进度条
const ivCo = iv.items.map((x) => x.co);
check('按公司一家一行（同一家不会重复出现）',
  new Set(ivCo).size === ivCo.length,
  JSON.stringify(ivCo.filter((c, i) => ivCo.indexOf(c) !== i)));
const ivRank = (x) => x.nodes.indexOf(x.cur);
// 两条口径分开量（2026-10-06 修）：排序是「活的按走得最远在前，止步的整段压在最后」
// （applytracker.py 的 ivShowList：sa - sb 先分两段，段内再按 rank 降序）。
// 老的写法拿整张表一路比下来 —— 她昨天新挂了两家（示例文具一面、示例日化测评），止步段一开头
// 就顶出个 rank 2，在老口径里看着像「倒挂」，其实是设计的。真正的坏味道是这两条：
// ① 活的段里倒挂 / ② 止步的没全收在末尾（掺一笔到活的中间去了）
const ivDesc = (arr) => arr.every((x, i) => i === 0 || ivRank(x) <= ivRank(arr[i - 1]));
const ivStopFrom = iv.items.findIndex((x) => x.stopped);
const ivSegLive = iv.items.filter((x) => !x.stopped);
const ivSegStop = iv.items.filter((x) => x.stopped);
check('走得最远的排在最前面（offer / 终面 先看见）；止步的整段收在最后、段内也按走得远排',
  iv.items.length > 0 && iv.items.every((x) => x.cur) &&
  ivDesc(ivSegLive) && ivDesc(ivSegStop) &&
  (ivStopFrom < 0 || iv.items.slice(ivStopFrom).every((x) => x.stopped)),
  JSON.stringify({ 活: ivSegLive.map((x) => [x.co, x.cur, ivRank(x)]).slice(0, 3),
                   止步段起点: ivStopFrom, 止步段: ivSegStop.map((x) => [x.co, x.cur, ivRank(x)]) }));
check('进度视图也有自己的几个数（在推进 / 拿到 offer / 已止步）',
  iv.kpis === 3 && iv.hint.indexOf('条通知') >= 0,
  JSON.stringify({ kpis: iv.kpis, hint: iv.hint }));
// 光靠 hover 提示她不会知道能点 —— 页面上得有一句白纸黑字（这块以前是空 div）
check('进度视图页面上写着「怎么点」（点已经过的环节 = 退回来）',
  iv.note.indexOf('退回来') >= 0 && iv.note.indexOf('往前推进') >= 0, iv.note.slice(0, 80));

const ivLive = iv.items.filter((x) => x.next > 0);
check('已达节点浅底、当前节点实心强调色、没到的折成一个灰块（紧挨当前那一档）',
  ivLive.length > 0 &&
  ivLive.every((x) => x.next <= 1 && (x.next === 0 || (!!x.dn && x.restList.length >= 1 && x.restList[0] === x.dn))) &&
  ivLive.every((x) => x.okBg !== x.nextBg && x.curBg !== x.nextBg) &&
  ivLive.some((x) => x.nextBorder.indexOf('dashed') >= 0),
  JSON.stringify(ivLive.slice(0, 3).map((x) => [x.nodes.length, x.ok, x.cur, x.dn, x.restList.length])));
check('未到的环节不止一个时，灰块写成「下一环节—offer」（她说的那种样子）；只剩一个就直接写它',
  ivLive.filter((x) => x.restList.length > 1).length > 0 &&
  ivLive.filter((x) => x.restList.length > 1).every((x) =>
    x.label.indexOf('—') >= 0 && x.label.slice(0, x.label.indexOf('—')) === x.dn &&
    x.label.slice(x.label.indexOf('—') + 1) === x.restList[x.restList.length - 1]),
  JSON.stringify(ivLive.map((x) => [x.restList.length, x.label, x.restList.join('/')]).slice(0, 4)));
// 当前节点铺的就是强调色。拿主按钮当参照 —— 不要拿颜色数值大小比深浅
// （深色主题下刚好反过来，那种断言会假红）
check('当前节点铺的是强调色（跟主按钮一个颜色）',
  !!iv.accentBg && ivLive.filter((x) => !x.ask && x.cur !== 'offer').length > 0 &&
  ivLive.filter((x) => !x.ask && x.cur !== 'offer').every((x) => x.curBg === iv.accentBg),
  JSON.stringify({ accent: iv.accentBg, cur: ivLive.slice(0, 3).map((x) => [x.cur, x.curBg]) }));

// 点灰块 = 把当前推进一档。写请求被拦着（返回假 ok），但**请求本身要真发出去**，
// 而且只能发到 /api/interview —— 顺便证明这条交互没有偷偷改到投递记录
const firstNext = ivLive.find((x) => x.next > 0);
if (firstNext) {
  await page.evaluate((co) => {
    const row = [...document.querySelectorAll('#ivList .ivrow')]
      .find((r) => ((r.querySelector('.ivco b') || {}).textContent || '').trim() === co);
    const el = row && row.querySelector('.ivn.next');
    if (el) el.click();
  }, firstNext.co);
  await sleep(900);
}
const ivWrites = (await page.evaluate(() => (window.__blocked || []).slice())).slice(blockedBefore);
check('点灰块能改当前环节，且只写 /api/interview（没碰投递记录）',
  ivWrites.length === 1 && /\/api\/interview$/.test(ivWrites[0]), JSON.stringify(ivWrites));

// 回退（她 2026-10-04：「选择进度时候也要可以回退」）——
// 原来 `.ivn.ok`（已经过的环节）**没有任何 handler**，点它跟点一块装饰没区别，
// 推进去了就再也退不回来。这条把它钉住：点了要真发 /api/interview，cur 是那个环节。
const ivBack = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const row = [...document.querySelectorAll('#ivList .ivrow')]
    .find((r) => r.querySelectorAll('.ivn.ok').length > 0);
  if (!row) return { why: '没有带「已经过」环节的行' };
  const el = row.querySelector('.ivn.ok');
  const node = el.dataset.node || '';
  const cur = (row.querySelector('.ivn.cur') || {}).dataset.node || '';
  const cursor = getComputedStyle(el).cursor;
  const before = (window.__wbody || []).length;
  el.click();
  await wait(1200);
  const w = (window.__wbody || []).slice(before);
  return { co: (row.querySelector('.ivco b') || {}).textContent.trim(), node: node, cur: cur,
           cursor: cursor, writes: w.map((x) => x.m + ' ' + x.u),
           body: w.length ? w[0].body : '',
           toast: (document.getElementById('toast') || {}).textContent || '' };
});
check('已经过的环节点一下能「退回来」（原来这格点了没反应）',
  !!ivBack.node && ivBack.node !== ivBack.cur && ivBack.cursor === 'pointer' &&
  ivBack.writes.length === 1 && /\/api\/interview$/.test(ivBack.writes[0]) &&
  ivBack.body.indexOf('"cur":"' + ivBack.node + '"') >= 0 &&
  ivBack.toast.indexOf('退回') >= 0,
  JSON.stringify(ivBack));

// 「改」：各家公司轮次不一样，得能自己排环节。展开的那条铺在公司那行下面、占满整行
const ivEdit = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const b = document.querySelector('#ivList .ivedit');
  if (!b) return null;
  // 按钮文字必须在点之前读 —— 点完这块会重铺，手里那个节点已经摘下来了，读到的还是旧文字
  const labelBefore = b.textContent.trim();
  b.click();
  await wait(600);
  const ed = document.querySelector('#ivList .ived');
  const box = document.getElementById('ivList');
  const r = {
    open: !!ed, hasInput: !!(ed && ed.querySelector('.ivnin')),
    edW: ed ? Math.round(ed.getBoundingClientRect().width) : -1,
    boxW: Math.round(box.getBoundingClientRect().width),
    btns: ed ? [...ed.querySelectorAll('button')].map((x) => x.textContent.trim()) : [],
    value: ed ? ed.querySelector('.ivnin').value : '',
    labelBefore: labelBefore,
    labelOpen: (document.querySelector('#ivList .ivedit') || {}).textContent || '',
  };
  if (ed) ed.querySelector('.ivcancel').click();
  await wait(500);
  r.closed = !document.querySelector('#ivList .ived');
  r.labelBack = (document.querySelector('#ivList .ivedit') || {}).textContent || '';
  return r;
});
check('「改」能展开（编辑器铺满整行、按钮从「改」变「收起」）、能取消收起来',
  !!ivEdit && ivEdit.open && ivEdit.hasInput && ivEdit.value.indexOf('、') >= 0 &&
  ivEdit.closed && ivEdit.edW >= ivEdit.boxW * 0.9 &&
  ivEdit.labelBefore === '改' && ivEdit.labelOpen.trim() === '收起' && ivEdit.labelBack.trim() === '改' &&
  ivEdit.btns.some((b) => b.indexOf('保存') >= 0) && ivEdit.btns.some((b) => b.indexOf('止步') >= 0),
  JSON.stringify(ivEdit));

// 进度条最容易在窄窗口把行撑破 —— 那一行里塞了四五个节点 + 一个「改」
for (const w of [1512, 1280, 1080, 980]) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(350);
  const r = await page.evaluate(() => {
    const bars = [...document.querySelectorAll('#ivList .ivbar')];
    const rows = [...document.querySelectorAll('#ivList .ivrow')];
    return {
      over: bars.filter((b) => b.scrollWidth > b.clientWidth + 1).length,
      rowOver: rows.filter((b) => b.scrollWidth > b.clientWidth + 1).length,
      sw: document.documentElement.scrollWidth, vw: window.innerWidth,
    };
  });
  check(`${w}px：进度条不溢出、整行没被撑破、页面也没有横向滚动`,
    r.over === 0 && r.rowOver === 0 && r.sw <= r.vw, JSON.stringify(r));
}

// —— v0.24.0 新增：进度视图也能筛（她 2026-10-05：「这一页也要可筛选进度」）——
// 筛选条 = 搜索框 + 环节标签 + 计数 + 清除。环节标签要按进度**先后**摆，不能按字母序
// —— 「笔试」按字序会跑到「一面」后面，看着就像「先面试后笔试」，那是错的。
await page.setViewport({ width: 1080, height: 900 });
await sleep(300);
const ivBar = await page.evaluate(() => {
  const vis = (el) => (el ? el.getBoundingClientRect().width > 0 : false);
  const q = document.getElementById('ivQ'), chips = document.getElementById('ivNodeChips');
  const cnt = document.getElementById('ivCount'), rst = document.getElementById('ivReset');
  return {
    q: vis(q), chips: vis(chips), cnt: vis(cnt),
    labels: chips ? [...chips.querySelectorAll('button')].map((b) => b.textContent.trim()) : [],
    cntText: cnt ? cnt.textContent.trim() : '',
    rstHidden: rst ? !!rst.hidden : null,
  };
});
check('投递进度那一版有筛选条（搜索框 / 环节标签 / 计数 / 清除），默认停在「全部」',
  ivBar.q && ivBar.chips && ivBar.cnt && ivBar.rstHidden === true &&
  ivBar.labels[0] === '全部' && /共 \d+ 家/.test(ivBar.cntText),
  JSON.stringify(ivBar));
// 进度先后：跟后端 INTERVIEW_ORDER 一个口径（不是拼音序、也不是节点出现序）
const IVORD = ['测评', '笔试', 'AI面试', '群面', '一面', '二面', '三面', '终面', 'HR面', 'offer'];
const ivoi = (l) => { const i = IVORD.indexOf(l); return i < 0 ? 999 : i; };
const ivStages = ivBar.labels.slice(1).filter((l) => l !== '已止步');
const ivStopAt = ivBar.labels.indexOf('已止步');
check('环节标签按进度先后摆（「笔试」不许跑到「一面」后面），「已止步」永远在最后',
  ivStages.every((l, i) => i === 0 || ivoi(l) >= ivoi(ivStages[i - 1])) &&
  (ivStopAt < 0 || ivStopAt === ivBar.labels.length - 1),
  JSON.stringify(ivBar.labels));

// 点环节标签 = 只看现在停在这一环的公司。用「每行的当前环节都等于它」来验，
// 比单看行数稳 —— 万一这一环刚好覆盖了所有公司，行数不变会假红
const ivPick = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const rows = () => [...document.querySelectorAll('#ivList .ivrow')];
  const btn = (t) => [...document.querySelectorAll('#ivNodeChips button')].find((b) => b.textContent.trim() === t);
  const labels = [...document.querySelectorAll('#ivNodeChips button')].map((b) => b.textContent.trim());
  const label = labels.find((l) => l !== '全部' && l !== '已止步');
  if (!label) return { why: '没有真实环节标签' };
  const before = { cnt: (document.getElementById('ivCount') || {}).textContent.trim(), n: rows().length };
  btn(label).click(); await wait(350);
  const mid = {
    n: rows().length, on: btn(label).classList.contains('on'),
    curs: rows().map((r) => { const c = r.querySelector('.ivn.cur'); return c ? c.dataset.node : ''; }),
    cnt: (document.getElementById('ivCount') || {}).textContent.trim(),
  };
  btn(label).click(); await wait(350);   // 再点一下 = 取消
  const back = { n: rows().length, allOn: btn('全部').classList.contains('on') };
  return { label, before, mid, back };
});
check('点环节标签能筛（只剩现在停在这一环的公司），再点一下取消回到「全部」',
  !!ivPick.mid && ivPick.mid.n > 0 && ivPick.mid.on && ivPick.mid.curs.every((c) => c === ivPick.label) &&
  ivPick.back.n === ivPick.before.n && ivPick.back.allOn === true,
  JSON.stringify(ivPick));

// 「已止步」只看挂了的 / 不再往下推的（她 2026-10-05：「示例涂料公司已经给我挂了」——
// 挂了的必须在这一档里看得见，不能因为「只停在测评的不列」那条把挂掉的也一起藏了）
const ivStop = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const btn = (t) => [...document.querySelectorAll('#ivNodeChips button')].find((b) => b.textContent.trim() === t);
  const chip = btn('已止步');
  if (!chip) return { why: '这一版没有「已止步」标签（此刻没有止步的公司）' };
  chip.click(); await wait(350);
  const rows = [...document.querySelectorAll('#ivList .ivrow')];
  const out = {
    n: rows.length,
    stopped: rows.filter((r) => r.classList.contains('stopped')).length,
    // 「投递表已结束」的说明文字与「止步 / 继续」按钮**互斥** —— 同一行不该两个都出现
    noteOnly: rows.filter((r) => r.querySelector('.ivhaltnote') && !r.querySelector('.ivhalt')).length,
    mutex: rows.every((r) => !(r.querySelector('.ivhaltnote') && r.querySelector('.ivhalt'))),
    noteText: ((rows.find((r) => r.querySelector('.ivhaltnote')) || {}).textContent || '').trim(),
  };
  btn('全部').click(); await wait(350);
  out.backN = document.querySelectorAll('#ivList .ivrow').length;
  return out;
});
check('「已止步」只列止步的公司 —— 挂了的这家在这一档看得见',
  ivStop.n > 0 && ivStop.n === ivStop.stopped, JSON.stringify(ivStop));
// 止步的源头分两种：手动点的 / 投递表标了「已结束」。后者不给「继续」按钮 ——
// 事实源头在投递表，这一页再给个「继续」就变成两套口径，只会越看越糊涂
check('止步来源是投递表「已结束」的行：写一行来由、不给「继续」按钮（不跟投递表打架）',
  ivStop.noteOnly > 0 && ivStop.mutex && ivStop.noteText.indexOf('投递表已结束') >= 0,
  JSON.stringify({ noteOnly: ivStop.noteOnly, mutex: ivStop.mutex, noteText: ivStop.noteText }));

// 她 2026-10-05：「已结束的在进度条后面加一个已挂，红色底色」。
// 原来止步只用「整行淡下去 + 行下一行小字」表达 —— 她的口径是「更美观 = 要看得见」，
// 淡下去恰恰是最看不见的做法。
const ivDeadRows = iv.items.filter((x) => x.dead);
check('挂了的在进度条末尾挂一个红「已挂」标；没挂的一个都不许有',
  ivDeadRows.length > 0 && iv.items.every((x) => (x.stopped ? !!x.dead && x.dead.txt === '已挂' : !x.dead)),
  JSON.stringify(iv.items.map((x) => [x.co, x.stopped, x.dead && x.dead.txt])));
check('红标是实心红（跟主题的 --red 一个色）、且没被「止步整行淡下去」一起淡掉',
  ivDeadRows.every((x) => x.dead.bd === iv.redRgb && x.dead.op === 1),
  JSON.stringify({ red: iv.redRgb, rows: ivDeadRows.map((x) => [x.co, x.dead.bd, x.dead.op]) }));

// 一键止步：只发 stop，**不带 nodes / cur** —— 带上就等于把这家的当前环节锁成「手动改过」，
// 以后通知再更新也推不动它了。这条断言就是钉这个（原来得点「改」再点「止步」两跳）
const ivHalt = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const b = document.querySelector('#ivList .ivhalt');
  if (!b) return { why: '没有可点的止步按钮' };
  const before = (window.__wbody || []).length;
  const label = b.textContent.trim(), key = b.dataset.key;
  b.click(); await wait(1200);
  const w = (window.__wbody || []).slice(before);
  return { label: label, key: key, writes: w.map((x) => x.m + ' ' + x.u),
           body: w.length ? w[0].body : '',
           toast: (document.getElementById('toast') || {}).textContent || '' };
});
check('一键止步：只发 /api/interview，体里只有 stop —— 不带 nodes / cur（不会把这家的当前环节锁死）',
  !!ivHalt.writes && ivHalt.writes.length === 1 && /\/api\/interview$/.test(ivHalt.writes[0]) &&
  /"stop":(true|false)/.test(ivHalt.body) &&
  ivHalt.body.indexOf('"nodes"') < 0 && ivHalt.body.indexOf('"cur"') < 0,
  JSON.stringify(ivHalt));

// 搜索框：公司名 / 岗位都能搜；清空要能回来，清除按钮要跟着亮
const ivSearch = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const q = document.getElementById('ivQ');
  const first = document.querySelector('#ivList .ivco b');
  if (!first) return { why: '没有行' };
  const name = first.textContent.trim();
  const beforeN = document.querySelectorAll('#ivList .ivrow').length;
  q.value = name; q.dispatchEvent(new Event('input', { bubbles: true })); await wait(350);
  const names = [...document.querySelectorAll('#ivList .ivco b')].map((b) => b.textContent.trim());
  const rst = document.getElementById('ivReset');
  const out = { name: name, beforeN: beforeN, n: names.length, names: names,
                rstShown: rst ? !rst.hidden : false };
  q.value = ''; q.dispatchEvent(new Event('input', { bubbles: true })); await wait(350);
  out.backN = document.querySelectorAll('#ivList .ivrow').length;
  out.backCnt = (document.getElementById('ivCount') || {}).textContent.trim();
  return out;
});
check('搜索框能按公司名筛、清除按钮会亮、清空就回来',
  ivSearch.n >= 1 && ivSearch.names.every((x) => x.indexOf(ivSearch.name) >= 0) &&
  ivSearch.rstShown === true && ivSearch.backN === ivSearch.beforeN && /共 \d+ 家/.test(ivSearch.backCnt),
  JSON.stringify(ivSearch));

// 筛选项记在浏览器里（下次打开还是这一档，不然每次都得重选）
const ivPersist = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const find = () => [...document.querySelectorAll('#ivNodeChips button')].find((b) => b.textContent.trim() === '已止步');
  if (!find()) return { why: '没有「已止步」标签' };
  find().click(); await wait(300);
  // ⚠️ 点完这排标签会被整块重铺，手里那个节点**已经摘下来了** —— 必须重新查。
  // 不然读到的是点**之前**的 on 状态（游离节点，量出来是旧的），会假红。
  const onNow = !!find() && find().classList.contains('on');
  let node = null; try { node = JSON.parse(localStorage.getItem('at.ivf.v1') || '{}').node; } catch (e) { /* 没存 */ }
  const out = { node: node, on: onNow };
  const rst = document.getElementById('ivReset');
  if (rst && !rst.hidden) rst.click();   // 复位，别把这一档带进后面
  await wait(300);
  return out;
});
check('筛选选择记在浏览器里（下次打开还在这一档，不用每次重选）',
  ivPersist.node === '__stop__' && ivPersist.on === true, JSON.stringify(ivPersist));
await sleep(300);

// 切回通知表，量那张 9 列表（上一版加了「面试进度」列就是在这儿撑出横向滚动的）
await page.evaluate(() => document.getElementById('ntvDl').click());
await sleep(500);
for (const w of [1512, 1280, 1080, 980]) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(350);
  const r = await page.evaluate(() => {
    const wrap = document.querySelector('#pane-notices .tbl-wrap');
    const tbl = document.querySelector('#pane-notices table');
    // 只量**看得见**的格子：窄屏会藏掉两列，display:none 的那几格宽是 0，
    // 把它们算进来这条断言必然假红（不是页面坏了）
    const cells = [...document.querySelectorAll('#ntTb tr:not(.sect) td')]
      .filter((c) => c.getBoundingClientRect().width > 0);
    return {
      tbOver: tbl.scrollWidth - wrap.clientWidth,
      cellMin: cells.length ? Math.round(Math.min(...cells.map((c) => c.getBoundingClientRect().width))) : -1,
      sw: document.documentElement.scrollWidth, vw: window.innerWidth,
    };
  });
  check(`${w}px：通知表没被撑出横向滚动、页面也没有`,
    r.tbOver <= 2 && r.sw <= r.vw && r.cellMin > 20, JSON.stringify(r));
}

// 她这轮提的第一件事：处理掉的不再报「过期 N 天」
// ⚠️ 2026-10-09 起要分两撮看：真处理掉的那些画破折号；**「标了已完成、但截止还没到」**的
// 反过来必须照常报剩余天数（它们本来就被她自己的「已完成」藏进灰区了，再画破折号等于二次埋掉）。
const dlOff = await page.evaluate(() => {
  const out = { done: 0, doneBad: [], check: 0, checkBad: [], open: 0, openOk: 0 };
  [...document.querySelectorAll('#ntTb tr:not(.sect)')].forEach((r) => {
    const sel = r.querySelector('td select.sts');
    const cells = [...r.querySelectorAll('td.dl')];
    const cell = cells[1];
    if (!sel || !cell) return;
    const txt = cell.textContent.trim();
    const closed = ['已完成', '已过期', '已忽略'].indexOf(sel.value) >= 0;
    const chk = r.className.indexOf('g-check') >= 0;
    if (closed) {
      if (chk) { out.check++; if (txt === '—') out.checkBad.push(r.querySelector('td.c-name').textContent); }
      else { out.done++; if (txt !== '—') out.doneBad.push(txt); }
    } else {
      out.open++;
      if (txt !== '—') out.openOk++;
    }
  });
  return out;
});
check('已经处理掉的通知不再报「过期 N 天」（距截止只画破折号），没处理的照旧报天数',
  dlOff.done > 0 && dlOff.doneBad.length === 0 && dlOff.open > 0 && dlOff.openOk > 0,
  JSON.stringify(dlOff));
check('「标了已完成、但截止还没到」的那撮反过来：照常报剩余天数（跟着画破折号 = 二次埋掉）',
  dlOff.check === 0 || dlOff.checkBad.length === 0,
  JSON.stringify({ check: dlOff.check, bad: dlOff.checkBad }));
await page.setViewport({ width: 1080, height: 900 });

// ---------- ⑦ 那排筛选 + 顶部精简 ----------
console.log('\n⑦ 筛选与顶部');
await fresh('notices', 'at.ntv.v1');
const ft = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const q = document.getElementById('ntQ');
  const set = async (v) => { q.value = v; q.dispatchEvent(new Event('input', { bubbles: true })); await wait(700); };
  await set('');
  const total = document.querySelectorAll('#ntTb tr').length;
  await set('zzz-没有这家公司-zzz');
  const rows = document.querySelectorAll('#ntTb tr').length;
  const cell = document.querySelector('#ntTb td.empty');
  const cntZero = (document.getElementById('ntCount') || {}).textContent || '';
  // ⚠️ 这颗按钮必须在**还处于空态时**取 —— 下面 set('') 一清搜索，空态单元格就没了，
  // 那之后再去 getElementById 永远拿到 null（2026-10-09 自己写错过一次）
  const emptyBtn = !!document.getElementById('ntClearEmpty');
  await set('');
  const back = document.querySelectorAll('#ntTb tr').length;
  const cntBack = (document.getElementById('ntCount') || {}).textContent || '';
  // 「只看没处理的」（她的原话：能单独看我现在还没处理的）：一点，表里就不该再有处理掉的行
  document.getElementById('ntOnlyTodo').click();
  await wait(800);
  const todoOn = document.getElementById('ntOnlyTodo').classList.contains('on');
  const anyDone = [...document.querySelectorAll('#ntTb td select.sts')]
    .some((sel) => ['已完成', '已过期', '已忽略'].indexOf(sel.value) >= 0);
  const todoRows = document.querySelectorAll('#ntTb tr:not(.sect)').length;
  document.getElementById('ntOnlyTodo').click();
  await wait(700);
  return {
    total: total, rows: rows, empty: !!cell, emptyTxt: cell ? cell.textContent : '',
    emptyBtn: emptyBtn,
    cntZero: cntZero, back: back, cntBack: cntBack,
    todoOn: todoOn, anyDone: anyDone, todoRows: todoRows,
    chips: document.querySelectorAll('#ntTypeChips button').length,
    hideDone: !!document.getElementById('ntHideDone'),
    sorts: ['ntSortUrg', 'ntSortDl', 'ntSortCo'].filter((i) => document.getElementById(i)).length,
    urgent: !!document.getElementById('ntUrgent'),
    hint: (document.getElementById('ntvHint') || {}).textContent || '',
  };
});
// v0.33.5：空态不再只是「试试清除筛选」—— 必须把**筛它的那个条件**原样念出来（她 2026-10-09
// 就是被一个她忘了的持久化类型标签筛成 0 条，然后去 QQ 邮箱自证「你漏了我邮件」），
// 并提供就地清除。所以这条断言从「有没有『清除筛选』四个字」加强成「有没有把条件念出来」。
check('搜不到就空表 + 如实写「筛出 0」并把**筛它的条件**念出来 + 就地能清，清空就回来',
  ft.total > 0 && ft.rows === 1 && ft.empty &&
  ft.emptyTxt.indexOf('zzz-没有这家公司-zzz') >= 0 && ft.emptyTxt.indexOf('筛着') >= 0 &&
  ft.emptyBtn &&
  ft.cntZero.indexOf('筛出 0') === 0 &&
  ft.cntZero.indexOf('zzz-没有这家公司-zzz') >= 0 &&
  ft.back === ft.total && ft.cntBack.indexOf('共 ') === 0,
  JSON.stringify(ft));
check('「只看没处理的」还能单独用：一点，已处理的行全部消失',
  ft.todoOn && !ft.anyDone && ft.todoRows > 0 && ft.todoRows < ft.total,
  JSON.stringify({ on: ft.todoOn, anyDone: ft.anyDone, todoRows: ft.todoRows, total: ft.total }));
check('类型标签 / 三个排序 / 隐藏已处理 / 只看紧急的 一件不少，切换条右边如实报数',
  ft.chips > 0 && ft.hideDone && ft.sorts === 3 && ft.urgent && ft.hint.indexOf('条通知') >= 0,
  JSON.stringify({ chips: ft.chips, sorts: ft.sorts, urgent: ft.urgent, hint: ft.hint }));

// 她嫌顶部乱：原来是「待办提示条 + 六个数字 + 三块分布」，重复的一堆；
// 现在砍成「三个数字 + 一块分布」，说明并进切换条右边那句
const top = await page.evaluate(() => ({
  kpis: [...document.querySelectorAll('#ntKpi .kpi-card')].map((k) => ((k.querySelector('span') || {}).textContent || '').trim()),
  panels: [...document.querySelectorAll('#ntDlView .panel h2')].map((h) => h.textContent.trim()),
  alert: !!document.getElementById('ntAlert'),
  oldSub: !!document.getElementById('ntSub'),
}));
check('顶部只剩 3 个数字 + 1 块分布（待办提示条和小标题都砍了）',
  top.kpis.length === 3 && top.panels.length === 1 && top.panels[0] === '通知类型分布' &&
  !top.alert && !top.oldSub, JSON.stringify(top));

// 顶部那排（视图切换条）窄窗口最容易把右边那句说明挤出界
for (const w of [1080, 980]) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(300);
  const r = await page.evaluate(() => {
    const t = document.querySelector('#pane-notices .nttabs');
    return { over: t ? t.scrollWidth - t.clientWidth : -1,
             sw: document.documentElement.scrollWidth, vw: window.innerWidth };
  });
  check(`${w}px：视图切换条不溢出、页面没有横向滚动`,
    r.over <= 1 && r.sw <= r.vw, JSON.stringify(r));
}
await page.setViewport({ width: 1080, height: 900 });
await page.evaluate(() => {
  const q = document.getElementById('ntQ');
  if (q) { q.value = ''; q.dispatchEvent(new Event('input', { bubbles: true })); }
});
await sleep(300);

// ⑥c 邮箱连接状态 + 连接弹卡（v0.33.4） ---------------------------------------
// 她 2026-10-08 的原话：「我输完连接完邮箱后之后这个还在。。」—— 两个独立的坑，各自的钉子：
//   ① 弹卡关不掉：#mailSetupWrap 的 CSS 写了 display:flex（**作者样式**），优先级高于
//      UA 的 [hidden]{display:none} → closeMailSetup() 里 hidden=true 形同虚设，弹卡带着
//      position:fixed + z-index:60 的全屏遮罩常显，三个标签页全点不动。
//      ⚠️ 必须用 getComputedStyle 实测「看不看得见」—— 读 el.hidden 会**假绿**，
//      这个 bug 的形态恰好就是「hidden=true 但照样显示」。
//   ② 连完看不见：/api/mail-setup 写了 [mail]、回了 ok，但她的 [notices] 源是 feishu、
//      一个字节没动，而旧的 mailBar() 在 feishu 模式下直接 el.hidden=true ——
//      连完邮箱整页一个字都不变。现在任何模式都要常驻一行邮箱连接状态。
console.log('\n⑥c 邮箱连接状态与连接弹卡');
// 这一组会故意点「改用直连收信 / 断开」—— 写请求被拦成假响应（返回 {ok:true}），
// 一个字节都落不到她的 config.ini 上；但**请求本身要真发出去**，所以末尾按次数对账。
let mailSrcExpected = 0, mailOffExpected = 0;
await fresh('notices', ['at.ntv.v1', 'at.ivf.v1']);
const mc = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const vis = (el) => !!el && getComputedStyle(el).display !== 'none';
  const wrap = document.getElementById('mailSetupWrap');
  const bar = document.getElementById('ntMailBar');
  window.__blocked = []; window.__wbody = [];
  await wait(400);
  const out = {
    mode: META.notices_mode,
    mailConfigured: !!META.mail_configured,
    mailEnabled: !!META.mail_enabled,
    patched: Array.isArray(window.__blocked) && /__blocked/.test(String(window.fetch)),
    wrapOnLoad: vis(wrap),                       // ← ① 的钉子：加载完必须是「看不见」
    wrapHiddenAttr: wrap ? wrap.hidden : null,
    barVisible: vis(bar),                        // ← ② 的钉子：任何模式都得有这一行
    barText: ((bar && bar.innerText) || '').replace(/\s+/g, ' ').trim(),
    hasUseBtn: !!document.getElementById('mailUseBtn'),
    hasOffBtn: !!document.getElementById('mailOffBtn'),
    hasSetupBtn: !!document.getElementById('mailSetupBtn'),
    hasSyncBtn: !!document.getElementById('mailSyncBtn'),
  };
  // 打开 → 必须真的出现；点「取消」→ 必须真的消失（把「打开→关闭」这条路径钉死）
  openMailSetup(); await wait(150);
  out.wrapOpen = vis(wrap);
  const cancel = document.getElementById('mstCancel');
  if (cancel) { cancel.click(); await wait(150); }
  out.wrapClosed = vis(wrap);
  out.wrapOpenAfterCancel = vis(document.getElementById('mailSetupWrap'));
  // 「改用直连收信」/「断开」：各点一次，看发的接口与字段。
  // ⚠️ 拦截桩没装到就**绝对不点** —— 那两下会真写她的 config.ini（见文件头上那道硬闸）。
  const use = document.getElementById('mailUseBtn');
  if (use && out.patched) {
    use.click(); await wait(250);
    out.useWrites = (window.__wbody || []).filter((w) => /\/api\/mail-source$/.test(w.u));
  } else if (use) { out.skippedUse = true; }
  const off = document.getElementById('mailOffBtn');
  if (off && out.patched) {
    off.click(); await wait(250);
    out.offWrites = (window.__wbody || []).filter((w) => /\/api\/mail-off$/.test(w.u));
  } else if (off) { out.skippedOff = true; }
  out.wrapAtEnd = vis(document.getElementById('mailSetupWrap'));
  return out;
});
check('⑥c 前置：页面上的写请求拦截桩装上了（没装上就不点那两个把手 —— 点了会真写她的 config.ini）',
  mc.patched === true, JSON.stringify({ patched: mc.patched, skippedUse: mc.skippedUse, skippedOff: mc.skippedOff }));
mailSrcExpected = (mc.useWrites || []).length;
mailOffExpected = (mc.offWrites || []).length;
check('连接弹卡：页面加载后是**看不见**的（作者 display 压过 [hidden] 会让它常显，盖住整页）',
  mc.wrapOnLoad === false && mc.wrapHiddenAttr === true,
  JSON.stringify({ wrapOnLoad: mc.wrapOnLoad, hiddenAttr: mc.wrapHiddenAttr }));
check('连接弹卡：点开真的出现、点「取消」真的消失（隐藏不是嘴上说说）',
  mc.wrapOpen === true && mc.wrapClosed === false && mc.wrapOpenAfterCancel === false,
  JSON.stringify({ open: mc.wrapOpen, closed: mc.wrapClosed, after: mc.wrapOpenAfterCancel }));
// 判据按「凭据 + 有没有被断开」**三分**（页面 mailBar() 本来就是三支，断言不能只取两支）：
//   ① 凭据在、在用   → 必须写着「已连上 / 已连接」（哪个邮箱也要写出来）
//   ② 凭据在、被断开 → 必须写着「已断开 + 重新连上」（授权码还留着，别让她再去 QQ 生成一遍）
//   ③ 完全没凭据     → 得给个入口（「没连邮箱」或 off 模式那段「邮箱通知还没接」）
// **不许**在飞书模式下整条藏起来，那正是她「连完邮箱一个字都没变」的原样。
// ⚠️ 只按「有没有凭据」二分会在她点过「断开」之后**永久假红**（2026-10-13 实测踩到）。
check('邮箱连接状态那一行在（飞书模式下也必须看得见 —— 她连完邮箱就得看见这句话）',
  mc.barVisible === true && !!mc.barText &&
  (mc.mailConfigured && mc.mailEnabled ? /(已连上|已连接)/.test(mc.barText)
   : mc.mailConfigured                 ? /已断开/.test(mc.barText) && /重新连上/.test(mc.barText)
                                       : /(没连邮箱|邮箱通知还没接)/.test(mc.barText)),
  JSON.stringify({ vis: mc.barVisible, 有凭据: mc.mailConfigured, 在用: mc.mailEnabled, txt: mc.barText }));
check('非直连模式：状态行说清「哪个邮箱 + 备用直连 + 当前读的是哪个源」并给两个把手',
  mc.mode === 'imap' || !mc.mailConfigured || !mc.mailEnabled ||
  (/已连上/.test(mc.barText) && /备用直连/.test(mc.barText) &&
   /(飞书表|本地 CSV|没接数据源)/.test(mc.barText) && mc.hasUseBtn && mc.hasOffBtn),
  JSON.stringify({ mode: mc.mode, 在用: mc.mailEnabled, txt: mc.barText, use: mc.hasUseBtn, off: mc.hasOffBtn }));
check('「改用直连收信」只发一次 POST /api/mail-source，且 source 字段就是 imap',
  !mc.hasUseBtn || (mc.useWrites && mc.useWrites.length === 1 &&
                    /"source"\s*:\s*"imap"/.test(mc.useWrites[0].body)),
  JSON.stringify(mc.useWrites));
check('「断开」只发一次 POST /api/mail-off',
  !mc.hasOffBtn || (mc.offWrites && mc.offWrites.length === 1), JSON.stringify(mc.offWrites));
// 这一行在 1080 / 980px 下不许把页面撑出横向滚动（她的窗口就是 1080）
for (const w of [1080, 980]) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(300);
  const r = await page.evaluate(() => {
    const b = document.getElementById('ntMailBar');
    return { over: b ? b.scrollWidth - b.clientWidth : -1,
             sw: document.documentElement.scrollWidth, vw: window.innerWidth };
  });
  check(`${w}px：邮箱连接状态那一行不溢出、页面没有横向滚动`,
    r.over <= 1 && r.sw <= r.vw, JSON.stringify(r));
}
await page.setViewport({ width: 1080, height: 900 });

// ⑥d 「按公司归纳」的公司行必须**横跨整行** -----------------------------------
// 2026-10-09 抓到的真 bug：flex 直接挂在 `<td colspan="9">` 上 → 这个格子**退出表格布局**，
// colspan 当场失效，宽度塌成第一列那点（实测 219 / 表宽 1050）→ 里面每个子元素被迫缩到极限，
// 四五个字的公司名被挤成**一个字一行**的竖条。眼睛扫一眼很容易当成「就这么设计的」，
// 所以必须**量**，不能看。修法：td 保持 display:table-cell，flex 挂到内层 div（.ntcor）上。
console.log('\n⑥d 归纳行横跨整行');
await page.evaluate(() => {
  const b = document.getElementById('ntGrouped');
  if (b && !b.classList.contains('on')) b.click();
});
await sleep(700);
const ntcoRow = await page.evaluate(() => {
  const tb = document.getElementById('ntTb');
  const tbl = tb.closest('table');
  const g = tb.querySelector('tr.ntco');
  if (!g) return { none: true };
  const td = g.querySelector('td');
  const box = td.querySelector('.ntcor');
  const btn = g.querySelector('.ntexp');
  const dl = g.querySelector('.ntdl');
  return {
    tableW: Math.round(tbl.getBoundingClientRect().width),
    tdW: Math.round(td.getBoundingClientRect().width),
    colspan: td.getAttribute('colspan'),
    tdDisplay: getComputedStyle(td).display,
    innerFlex: box ? getComputedStyle(box).display : '(缺 .ntcor)',
    nameH: Math.round(btn.getBoundingClientRect().height),
    nameW: Math.round(btn.getBoundingClientRect().width),
    name: (btn.textContent || '').trim().slice(0, 12),
    dlRight: dl ? Math.round(dl.getBoundingClientRect().right) : -1,
  };
});
check('归纳的公司行横跨整行（flex 挂内层 div，td 必须是 display:table-cell，宽度≈表宽）',
  !ntcoRow.none && ntcoRow.tdDisplay === 'table-cell' && ntcoRow.innerFlex === 'flex' &&
  ntcoRow.colspan === '9' && ntcoRow.tdW >= ntcoRow.tableW - 4,
  JSON.stringify(ntcoRow));
check('归纳行里公司名没被挤成竖条（高度就是一行、宽度写得下名字；截止时间被推到行尾）',
  !ntcoRow.none && ntcoRow.nameH <= 34 && ntcoRow.nameW >= 60 &&
  (ntcoRow.dlRight === -1 || ntcoRow.dlRight >= ntcoRow.tableW - 30),
  JSON.stringify(ntcoRow));

// ⑥e 平铺形态的**分段标题行**也必须横跨整行 -------------------------------------
// 同一条铁律的另一半，2026-10-09 量归纳行时顺手抓出来的**老**bug：
// `tbody tr.sect td` 一直写着 `display:flex` → 实测宽度 219 / 表宽 1050，
// 「今天截止 / 3 天内截止 / 已经处理掉的」这些标题被压成窄窄一条。肉眼同样看不出来。
console.log('\n⑥e 分段行（平铺形态）横跨整行');
await page.evaluate(() => {
  const b = document.getElementById('ntGrouped');
  if (b && b.classList.contains('on')) b.click();   // 切到平铺
});
await sleep(700);
const sectRow = await page.evaluate(() => {
  const tb = document.getElementById('ntTb');
  const tbl = tb.closest('table');
  const s = tb.querySelector('tr.sect');
  if (!s) return { none: true };
  const td = s.querySelector('td');
  const box = td.querySelector('.sectr');
  const sn = s.querySelector('.sn');
  return {
    tableW: Math.round(tbl.getBoundingClientRect().width),
    tdW: Math.round(td.getBoundingClientRect().width),
    colspan: td.getAttribute('colspan'),
    tdDisplay: getComputedStyle(td).display,
    innerFlex: box ? getComputedStyle(box).display : '(缺 .sectr)',
    label: (s.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 24),
    snRight: sn ? Math.round(sn.getBoundingClientRect().right) : -1,
  };
});
check('平铺形态的分段标题行横跨整行（flex 挂内层 .sectr，td 是 table-cell，宽度≈表宽）',
  !sectRow.none && sectRow.tdDisplay === 'table-cell' && sectRow.innerFlex === 'flex' &&
  sectRow.colspan === '9' && sectRow.tdW >= sectRow.tableW - 4,
  JSON.stringify(sectRow));
check('分段行的「N 条」被推到行尾（flex 没失效的旁证）',
  !sectRow.none && (sectRow.snRight === -1 || sectRow.snRight >= sectRow.tableW - 30),
  JSON.stringify(sectRow));
await page.evaluate(() => {
  const b = document.getElementById('ntGrouped');
  if (b && !b.classList.contains('on')) b.click();   // 切回她拍板的归纳形态
});
await sleep(700);

// ⑥f 「筛出 0」必须说清是**哪几个条件**把它筛没的 + 就地能清 ---------------------
// 她 2026-10-09 的原话：「不是你怎么又给我漏邮件了？？」——现场是类型标签**持久化**在
// localStorage 里（她早先点过「资料补充」），搜索框填某家公司名 → 当场 0 条，
// 而那条通知恰恰是「测评」。空表一声不响 = 她只能去 QQ 邮箱自证「你漏了」。
console.log('\n⑥f 筛出 0 条要说清条件');
const emptyWhy = await page.evaluate(() => {
  const all = NOTICES || [];
  const types = [...new Set(all.flatMap((x) => x['通知类型'] || []))];
  const coList = [...new Set(all.filter((x) => (x['通知类型'] || []).length && x['公司名称'])
    .map((x) => x['公司名称']))];
  let picked = null, out = null;
  for (const co of coList) {
    const mine = new Set(all.filter((x) => x['公司名称'] === co)
      .flatMap((x) => x['通知类型'] || []));
    const t2 = types.find((t) => !mine.has(t));
    if (!t2) continue;
    ntClearFilters();
    ntType = t2; ntQ = co;
    document.getElementById('ntQ').value = co;
    renderNotices();
    if (document.querySelectorAll('#ntTb tr.ntco').length === 0) { picked = { co, t2 }; break; }
  }
  if (!picked) return { none: true };
  const emp = document.querySelector('#ntTb .empty');
  const btn = document.getElementById('ntClearEmpty');
  out = {
    co: picked.co, type: picked.t2,
    rows: document.querySelectorAll('#ntTb tr.ntco').length,
    emptyText: emp ? (emp.innerText || '').replace(/\s+/g, ' ') : '(空态单元格都没有)',
    hasBtn: !!btn,
    cntText: (document.getElementById('ntCount') || {}).textContent || '',
    resetTxt: (document.getElementById('ntReset') || {}).textContent || '',
  };
  if (btn) btn.click();
  out.afterRows = document.querySelectorAll('#ntTb tr.ntco').length;
  out.afterType = ntType; out.afterQ = ntQ;
  out.afterCnt = (document.getElementById('ntCount') || {}).textContent || '';
  return out;
});
check('「搜一个公司 + 叠一个它没有的类型」确实能筛成 0 条（先证明这组条件真为空）',
  !emptyWhy.none && emptyWhy.rows === 0, JSON.stringify(emptyWhy).slice(0, 200));
check('筛出 0 条时空态把生效条件一条条念出来（搜索词 + 类型名都要出现在表里）',
  !emptyWhy.none && emptyWhy.emptyText.indexOf('当前筛着') >= 0 &&
  emptyWhy.emptyText.indexOf(emptyWhy.co) >= 0 && emptyWhy.emptyText.indexOf(emptyWhy.type) >= 0,
  emptyWhy.emptyText);
check('筛出 0 条时给了一颗就地「清除全部筛选」的按钮，点一下真回到全部（不是摆设）',
  !emptyWhy.none && emptyWhy.hasBtn && emptyWhy.afterType === null && emptyWhy.afterQ === '' &&
  emptyWhy.afterRows > 0,
  JSON.stringify(emptyWhy).slice(0, 200));
check('计数行也把生效条件念出来（她扫一眼计数就知道表正被筛着）',
  !emptyWhy.none && emptyWhy.cntText.indexOf('筛出') >= 0 &&
  emptyWhy.cntText.indexOf(emptyWhy.co) >= 0 && emptyWhy.cntText.indexOf(emptyWhy.type) >= 0 &&
  emptyWhy.afterCnt.indexOf(emptyWhy.co) < 0,
  emptyWhy.cntText + ' → ' + emptyWhy.afterCnt);
check('「清除筛选」按钮在有筛选时带出「几项」（她自己点过的东西会忘，数字会提醒）',
  !emptyWhy.none && emptyWhy.resetTxt.indexOf('2 项') >= 0,
  emptyWhy.resetTxt);

// ⑥g 「标了已完成、但截止还没到」必须单独成段、而且看得见 -------------------------
// 她 2026-10-09 **连着两次**报「你又给我漏邮件了？？」，两次都落在同一条缝里：
// 她自己把那行标成「已完成」→ 既不计入「还没处理」、又整行灰字沉到 40 家里最难看见的位置；
// 可那行的**截止时间根本还没到**（当天 21:00 截止的笔试通知、次日 10:21 截止的测评通知）。
// 对她来说「已完成」更像「我看过了」，不等于「时间上这件事结束了」。判据见 ntOpenDone()。
console.log('\n⑥g 标了已完成但截止还没到的，看得见');
const chkSeg = await page.evaluate(() => {
  ntClearFilters();
  const rows = (NOTICES || []).filter((r) => ntOpenDone(r));
  const secs = [...document.querySelectorAll('#ntTb tr.sect')];
  const seg = secs.find((s) => s.className.indexOf('g-check') >= 0);
  const kpiCards = [...document.querySelectorAll('#ntKpi .kpi-card')];
  const out = {
    n: rows.length,
    hasSeg: !!seg,
    kpiDone: (kpiCards[2] || {}).textContent || '',
    hint: (document.getElementById('ntvHint') || {}).textContent || '',
  };
  if (!rows.length) return out;
  out.segTxt = seg ? (seg.innerText || '').replace(/\s+/g, ' ') : '(没有这一段)';
  out.segDisplay = seg ? getComputedStyle(seg.querySelector('td')).display : null;
  out.segTdW = seg ? Math.round(seg.querySelector('td').getBoundingClientRect().width) : -1;
  out.segTableW = seg ? Math.round(seg.closest('table').getBoundingClientRect().width) : -1;
  out.checkIdx = secs.map((s) => s.className).findIndex((c) => c.indexOf('g-check') >= 0);
  out.doneIdx = secs.map((s) => s.className).findIndex((c) => c.indexOf('g-done') >= 0);
  // 段里第一条明细行：必须是「不灰 + 距截止写剩余天数」
  const first = rows[0];
  const tr = [...document.querySelectorAll('#ntTb tr')].find((t) =>
    !t.classList.contains('sect') && t.className.indexOf('g-check') >= 0 &&
    ((t.querySelector('td.c-name') || {}).textContent || '') === first['公司名称']);
  out.rowCls = tr ? tr.className : null;
  out.dlTxt = tr ? ((tr.querySelector('td.dl') || {}).textContent || '') : null;
  // ⚠️ 段头说的和底下列的必须是**同一撮**。第一版是「遇到第一家 check 就插段头」，
  // 结果一家「待处理 + 已完成未到期」混着的公司把段头提前引到它头上，
  // 紧跟其后的「没识别出截止」那两家就掉进了标题底下。
  const all = [...document.querySelectorAll('#ntTb tr')];
  const iChk = all.findIndex((t) => t.classList.contains('sect') && t.className.indexOf('g-check') >= 0);
  const kidsOf = (i) => {
    const k = [];
    for (let j = i + 1; j < all.length; j++) {
      if (all[j].classList.contains('sect') || all[j].classList.contains('ntco')) break;
      k.push(all[j]);
    }
    return k;
  };
  const body = [];
  for (let j = iChk + 1; j < all.length && !all[j].classList.contains('sect'); j++) {
    if (all[j].classList.contains('ntco')) body.push(all[j]);
  }
  out.secCos = body.length;
  out.secHeadN = Number(((out.segTxt || '').match(/([0-9]+) 家/) || [])[1] || -1);
  out.allHaveChk = body.length > 0 && body.every((g) =>
    kidsOf(all.indexOf(g)).some((x) => x.className.indexOf('g-check') >= 0));
  out.secNames = body.map((g) => (g.querySelector('.ntexp') || {}).textContent || '');
  return out;
});
check('「标了已完成、但截止还没到」单独成段、排在「已经处理掉的」之前、横跨整行',
  chkSeg.n === 0 || (chkSeg.hasSeg && chkSeg.checkIdx >= 0 &&
    (chkSeg.doneIdx < 0 || chkSeg.checkIdx < chkSeg.doneIdx) &&
    chkSeg.segDisplay === 'table-cell' && chkSeg.segTdW >= chkSeg.segTableW - 4),
  JSON.stringify(chkSeg).slice(0, 240));
check('那一段的明细行不灰、距截止报的是剩余天数（灰掉正是她两次看不见它的原因）',
  chkSeg.n === 0 || (!!chkSeg.rowCls && chkSeg.rowCls.indexOf('done') < 0 &&
    !!chkSeg.dlTxt && chkSeg.dlTxt !== '—'),
  JSON.stringify({ cls: chkSeg.rowCls, dl: chkSeg.dlTxt }).slice(0, 200));
check('「已处理」那张卡与视图切换条都报出这撮数（她两次都是从这两个地方开始找表的）',
  chkSeg.n === 0 || (chkSeg.kpiDone.indexOf('条截止还没到') >= 0 &&
    chkSeg.hint.indexOf('条标了已完成但截止还没到') >= 0),
  JSON.stringify({ kpi: chkSeg.kpiDone, hint: chkSeg.hint }).slice(0, 240));
check('那一段底下的每一家都真带一条「已完成但还没到期」的行，家数也跟段头对得上（标题 ≠ 底下的行是两回事）',
  chkSeg.n === 0 || (chkSeg.secCos > 0 && chkSeg.secCos === chkSeg.secHeadN && chkSeg.allHaveChk),
  JSON.stringify({ cos: chkSeg.secCos, head: chkSeg.secHeadN, ok: chkSeg.allHaveChk, names: chkSeg.secNames }).slice(0, 280));

// ⑦ 页面自己发现「我这份代码过期了」 -------------------------------------------
// 她那个标签页常常是几小时前打开的（页脚版本只在加载时渲染一次），后台升级了服务、
// 她那边照旧跑老代码，表现就是「你改了？我怎么没看见」。所以版本对不上必须由页面自己发现。
const verBase = await page.evaluate(() => {
  const c = document.getElementById('verNew');
  return { page: PAGE_VERSION, srv: META.version, chip: !!c, hidden: c ? c.hidden : null, stale: verStale() };
});
check('刚加载的页面，自己印的版本跟服务端当前版本一致（页面不该是旧的）',
  !!verBase.page && verBase.page === verBase.srv && verBase.stale === false, JSON.stringify(verBase));
check('刷新提示条平时藏着（不该无缘无故冒出来）', verBase.chip && verBase.hidden === true, JSON.stringify(verBase));

// 真跑一遍「旧代码」流程：把页面自己的版本改掉，并把 verDoReload 打桩 —— 不然就是真刷新，
// 后面的用例全废。两处调用点都**同步连着调两次 verTick**：只该排一次刷新（verReloaded 兜住），
// 这样断言是确定的，不会因为 10 秒轮询刚好插进来而飘。
const verFlow = await page.evaluate(async () => {
  const real = PAGE_VERSION, chip = document.getElementById('verNew');
  const orig = window.verDoReload;
  let hit = 0, d = null;
  window.verDoReload = () => { hit++; };
  try {
    PAGE_VERSION = '0.0.0-test';        // 装成「手里这页是旧代码」
    verReloaded = false; verTick(); verTick();
    await new Promise((r) => setTimeout(r, 1200));
    const rect = chip.getBoundingClientRect();
    const out = { idleHit: hit, shown: !chip.hidden, txt: chip.textContent.trim(),
                  inView: rect.left >= 0 && rect.right <= window.innerWidth + 1,
                  hiddenThen: chip.hidden };
    // 正在改某一格的时候绝不许刷 —— 刷了就吞掉她敲进去的东西
    hit = 0; verReloaded = false;
    d = document.createElement('td'); d.className = 'editing'; document.body.appendChild(d);
    verTick(); verTick();
    await new Promise((r) => setTimeout(r, 1200));
    out.editHit = hit;
    out.shownWhileEdit = !chip.hidden;
    return out;
  } finally {
    if (d) d.remove();
    PAGE_VERSION = real; verReloaded = false; verTick();   // 还原，别把后面的用例带跑
    window.verDoReload = orig;
  }
});
check('版本对不上 → 亮出提示条，写明服务端的新版本号、写着「点这里刷新」、且不出视口',
  verFlow.shown && verFlow.hiddenThen === false && /v\d+\.\d+\.\d+/.test(verFlow.txt) &&
  verFlow.txt.indexOf('刷新') >= 0 && verFlow.inView, JSON.stringify(verFlow));
check('版本对不上且她没在敲东西 → 自动刷新**一次**（连调两次也只排一次）',
  verFlow.idleHit === 1, JSON.stringify(verFlow));
check('正在改某一格时**不**刷新（她敲到一半绝不能被刷掉），提示条留着',
  verFlow.editHit === 0 && verFlow.shownWhileEdit === true, JSON.stringify(verFlow));
const verAfter = await page.evaluate(() => ({ hidden: document.getElementById('verNew').hidden, stale: verStale() }));
check('版本对上之后，提示条自己收回去', verAfter.hidden === true && verAfter.stale === false, JSON.stringify(verAfter));

// ---------- ⑨ 机会库：同一家公司的分行并成一行 ----------
// 她的原话：「把同一家公司不同分行放在同一个公司下面」。源表把一家银行拆成几十条
// （邮储 30 多个「XX省/市分行」、招行 50 多、中信期货 40 多），一条一行的话整页全是
// 同一家公司的名字。现在一家占一行、点开才摊开。归并是**后端**做的（先按家分组再分页），
// 所以这里还要验一件事：翻页不能把一家劈到两页去。
console.log('\n⑨ 机会库按公司归并');
await fresh('opps');

const gp = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const all = () => [...document.querySelectorAll('#oppTb tr')];
  const grps = () => all().filter((r) => r.classList.contains('grp'));
  const subs = () => all().filter((r) => r.classList.contains('sub'));
  const plain = () => all().filter((r) => !r.classList.contains('grp') && !r.classList.contains('sub'));
  const txt = (tr, sel) => ((tr.querySelector(sel) || {}).textContent || '').trim();
  const num = (tr) => +(((tr.querySelector('.gn') || {}).textContent || '').replace(/\D+/g, '') || 0);
  const card = (sel) => (document.querySelector('#oppKpi .kpi-card[data-q="cur"] ' + sel) || {}).textContent || '';
  const out = {
    grp: grps().length, plain: plain().length, subsClosed: subs().length,
    names: grps().map((r) => txt(r, '.gtoggle span:last-child')),
    gn: grps().map((r) => txt(r, '.gn')),
    n: grps().map(num),
    pos: grps().map((r) => txt(r, '.gpos')),
    arw: grps().map((r) => !!r.querySelector('.gtoggle .arw')),
    count: document.getElementById('oppCount').textContent,
    kpiCur: card('b'), kpiSub: card('em'),
    moreBar: !document.getElementById('oppMoreBar').hidden,
    moreTxt: (document.getElementById('oppMoreBtn') || {}).textContent || '',
    note: document.getElementById('oppNote').textContent,
  };
  // 点一下组头（不是只能点小三角）→ 摊开；再点一下 → 收起
  const g0 = grps()[0];
  if (g0) {
    out.firstName = txt(g0, '.gtoggle span:last-child');
    g0.querySelector('td.c-name').click();
    await wait(300);
    out.openedSubs = subs().length;
    out.openedDots = subs().filter((r) => !!r.querySelector('.subdot')).length;
    const g1 = grps()[0];
    g1.querySelector('td.c-name').click();
    await wait(300);
    out.closedSubs = subs().length;
  }
  // 组头里的标签（「2027届」/「国有企业」）只该筛，不该顺手把这家摊开
  const t = grps().find((r) => r.querySelector('.cellf'));
  if (t) {
    out.tagBefore = subs().length;
    t.querySelector('.cellf').click();
    await wait(1000);
    out.tagSubs = subs().length;
    out.tagReset = !document.getElementById('oppReset').hidden;
    out.tagCount = document.getElementById('oppCount').textContent;
  }
  return out;
});
check('一家公司只占一行：主行公司名互不重复，顺手也说了这家有几条（N 个分行 / N 个岗位）',
  gp.grp > 0 && gp.plain > 0 && gp.names.every((n) => n) &&
  new Set(gp.names).size === gp.names.length &&
  gp.gn.every((s) => /^\d+ 个(分行|岗位)$/.test(s)) &&
  gp.gn.length === gp.grp,
  JSON.stringify({ grp: gp.grp, plain: gp.plain, gn: gp.gn.slice(0, 4),
                   dup: gp.names.length - new Set(gp.names).size }));
check('折叠态里一行就是一家（分行没铺出来），点一下组头摊开正好这么多行、再点收起',
  gp.subsClosed === 0 && gp.openedSubs === gp.n[0] && gp.closedSubs === 0 &&
  gp.openedDots === gp.openedSubs,
  JSON.stringify({ closed: gp.subsClosed, opened: gp.openedSubs, n: gp.n[0], back: gp.closedSubs }));
check('组头第二行写着各分行/岗位的短名，前面有小三角，一眼看得出能点开',
  gp.pos.length === gp.grp && gp.pos.every((s) => s.length > 0) && gp.arw.every(Boolean),
  JSON.stringify({ pos: gp.pos.slice(0, 3), arw: gp.arw.filter(Boolean).length }));
check('点组头里的标签只按它筛（不会顺手把这家摊开），筛完出现「清除筛选」',
  gp.tagBefore === 0 && gp.tagSubs === 0 && gp.tagReset === true,
  JSON.stringify({ before: gp.tagBefore, after: gp.tagSubs, reset: gp.tagReset }));
check('右下角计数同时说清两个口径（家数 + 条数），跟 KPI「筛出公司」卡是同一个数',
  gp.count.startsWith('筛出 ' + gp.kpiCur + ' 家（') && /条岗位）$/.test(gp.count.trim()) &&
  gp.count.indexOf(gp.kpiSub.replace(/\D+/g, '')) >= 0,
  JSON.stringify({ count: gp.count, kpi: gp.kpiCur, sub: gp.kpiSub }));
check('说明里写清「一家公司只占一行 / 点这一行就摊开」，「再看 60 家」按家翻',
  gp.note.indexOf('一家公司只占一行') >= 0 && gp.note.indexOf('点这一行就摊开') >= 0 &&
  gp.moreBar && gp.moreTxt.indexOf('家') > 0,
  JSON.stringify({ more: gp.moreTxt, bar: gp.moreBar }));

// 分页按「家」切：每一家要么整家在、要么整家不在 —— 先把筛选清掉，再看两批
// ⚠️ 这里必须先 fresh() 清一次存档（at.opps.q.v1 连着「再看几屏」的翻页记忆一起清）：
//    上面的归并用例为了找到目标公司会把「再看 60 家」点大，而且这个页数**存档**、
//    重载也还在 —— 不清的话轮到分页用例时一页已经装得下全部 67 家，
//    翻页条根本不出现，「批2 比 批1 多」这条就永远过不了（数据一大必踩，2026-10-08）。
await fresh('opps', 'at.opps.q.v1');
const pg = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const rst = document.getElementById('oppReset');
  if (rst && !rst.hidden) { rst.click(); await wait(1000); }
  const expand = (on) => {
    Object.keys(OPP_GRP).forEach((k) => delete OPP_GRP[k]);
    if (on) OPP.groups.forEach((g) => { if (g.n > 1) OPP_GRP[g.key] = true; });
    renderOpps();
  };
  // 摊开后逐行扫：每家组头后面跟的「子行」条数必须正好等于它自己写的条数
  const scan = () => {
    const seen = { grp: [], subs: 0, plain: 0 };
    let cur = null;
    [...document.querySelectorAll('#oppTb tr')].forEach((r) => {
      if (r.classList.contains('grp')) {
        cur = { n: +(((r.querySelector('.gn') || {}).textContent || '').replace(/\D+/g, '') || 0), subs: 0 };
        seen.grp.push(cur);
      } else if (r.classList.contains('sub')) { if (cur) cur.subs++; seen.subs++; }
      else { cur = null; seen.plain++; }
    });
    return seen;
  };
  expand(false);
  await wait(200);
  const folded = scan();
  expand(true);
  await wait(400);
  const s1 = scan(), rows1 = document.querySelectorAll('#oppTb tr').length;
  const b = { groups: OPP.groups.length, rows: OPP.total_rows, all: OPP.all };
  expand(false);
  await wait(200);
  document.getElementById('oppMoreBtn').click();
  await wait(1400);
  expand(true);
  await wait(400);
  const s2 = scan(), rows2 = document.querySelectorAll('#oppTb tr').length;
  const a = { groups: OPP.groups.length, rows: OPP.total_rows, txt: document.getElementById('oppCount').textContent };
  expand(false);
  await wait(200);
  document.getElementById('oppReset').click();
  await wait(1000);
  return {
    foldedSubs: folded.subs, foldedGrp: folded.grp.length,
    bad1: s1.grp.filter((x) => x.subs !== x.n), n1: s1.grp.length,
    sum1: s1.grp.reduce((t, x) => t + x.n, 0), rows1: rows1, plain1: s1.plain,
    bad2: s2.grp.filter((x) => x.subs !== x.n), n2: s2.grp.length,
    sum2: s2.grp.reduce((t, x) => t + x.n, 0), rows2: rows2, plain2: s2.plain,
    b: b, a: a,
  };
});
check('摊开后逐行对得上：每家主行后面跟的记录条数 = 主行写的条数，整页行数也正好合得上',
  pg.foldedSubs === 0 && pg.n1 > 0 && pg.bad1.length === 0 &&
  pg.rows1 === pg.n1 + pg.sum1 + pg.plain1,
  JSON.stringify({ bad: pg.bad1.slice(0, 2), n: pg.n1, sum: pg.sum1, rows: pg.rows1, plain: pg.plain1 })
    .slice(0, 220));
// ⚠️ 这条原来要求 pg.n2 > pg.n1（翻下一批必须多出「多条记录的公司」）。那条判据**依赖数据**：
//    它默认第 61~120 家里至少有一家是多条的。2026-10-10 机会库换了快照，按截止时间排下来
//    第 61~120 家全是「一家一条」，下一家多条公司要排到 120 名之后 —— 渲染本身没问题
//    （实测 60 / 120 / 180 三批，domGrp 恰等于 groups 里 n>1 的家数，逐批对得上），
//    红的是这条断言的前提，不是代码。
//    换成不依赖数据的强判据：第二批必须**真的多铺了行**（总行数、单条公司的行数都得涨），
//    「整家不劈到两页上」照旧是硬条件。
check('分页按「家」切：翻下一批后每一家照样是整家在，一家都没被劈到两页上',
  pg.bad2.length === 0 && pg.n2 >= pg.n1 && pg.rows2 > pg.rows1 && pg.plain2 > pg.plain1 &&
  pg.a.groups > pg.b.groups &&
  pg.rows2 === pg.n2 + pg.sum2 + pg.plain2 && pg.a.rows === pg.b.rows,
  JSON.stringify({ bad: pg.bad2.slice(0, 2), 批1: pg.n1 + ' 家', 批2: pg.n2 + ' 家',
                   rows1: pg.rows1, rows2: pg.rows2, plain1: pg.plain1, plain2: pg.plain2,
                   sum: pg.sum2 }).slice(0, 260));

// 她那台机器的窗口只有 1080px —— 一张多了「组头 + 子行」的表最容易在这儿横向撑破
for (const w of [1080, 980]) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(400);
  const r = await page.evaluate(() => {
    const tb = document.getElementById('oppTb');
    const wrap = tb.closest('div') || tb.parentElement;
    const wide = [...document.querySelectorAll('#oppTb tr')].filter(
      (tr) => tr.getBoundingClientRect().width > wrap.clientWidth + 1).length;
    return { sw: document.documentElement.scrollWidth, vw: window.innerWidth,
             tbOver: tb.scrollWidth - tb.clientWidth, wide: wide };
  });
  check(`${w}px：机会库的表格不横向溢出（组头 / 子行都没把列撑宽）`,
    r.sw <= r.vw && r.tbOver <= 1 && r.wide === 0, JSON.stringify(r));
}
await page.setViewport({ width: 1080, height: 900 });

// ── ⑩ 投递进度表：把子公司并到主公司下面（「并入 / 拆出」） ──────────────────────
// 她 2026-10-07 的原话：「给我权限，我能自动把子公司归并到主公司下面」——
// 示例五局 / 示例南洋 / 示例国际城市开发 那几行收进「示例建筑集团」一行底下。
// 程序**不猜**母子关系（「中国」开头的大公司一堆，按前缀并全糊成一家；「示例家居」跟
// 「示例家居集团」比真母子还像），只认她自己点过的那条 —— 所以这里量的是「点一下到底发生了什么」。
await fresh('apps', ['at.apps.q.v1', 'at.apps.open.v1']);
await sleep(1400);
console.log('\n⑩ 投递进度表：子公司并入主公司');
const NEWCO = '体检用主公司';   // 故意用一个她表里没有的名字：不跟真实公司撞、也顺带验「主公司名可以现打」
const mg = await page.evaluate(async (NEWCO) => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const rows = () => [...document.querySelectorAll('#appTb tr')];
  const subRows = () => rows().filter((r) => r.classList.contains('sub'));
  const coRows = () => rows().filter((r) => !r.classList.contains('sub'));
  const btns = () => [...document.querySelectorAll('#appTb button.mg')];
  // ⚠️ 2026-10-10 起，概览那几行被砍成两行（她：「不要下面的文字」），**其余的数挪进了 `title`** ——
  //    所以这里 textContent 必须**连 title 一起读**，否则「概览『已归并子公司』+1」那两条会假红。
  const ovText = () => { const o = document.getElementById('appOverview');
                         return (o.textContent + ' ' + (o.title || '')).replace(/\s+/g, ' '); };
  const ovSubs = (t) => {                       // 概览里「已归并子公司 N 家」
    const m = t.match(/已归并子公司\s*(\d+)\s*家/);
    return m ? +m[1] : 0;
  };
  const out = {};
  const table = document.getElementById('appTb').closest('table');
  out.rowsBefore = coRows().length;
  out.subsBefore = ovSubs(ovText());
  out.btnCount = btns().length;
  out.btnLabels = [...new Set(btns().map((b) => b.textContent.trim()))];
  out.btnInOpCell = btns().filter((b) => b.closest('td').classList.contains('c-op')).length;
  out.btnDisplay = [...new Set(btns().map((b) => getComputedStyle(b).display))];
  out.subHasBtn = subRows().filter((r) => r.querySelector('button.mg')).length;
  // 按钮有没有把「操作」那列撑宽：藏起来再量一次（这张表 1080px 下横向一个像素余量都没有）
  const opW = () => Math.round(table.querySelector('thead th:last-child').getBoundingClientRect().width);
  const tW = () => Math.round(table.getBoundingClientRect().width);
  const shown = { t: tW(), op: opW() };
  btns().forEach((b) => { b.style.visibility = 'hidden'; });
  await wait(60);
  out.colShown = shown;
  out.colHidden = { t: tW(), op: opW() };
  btns().forEach((b) => { b.style.visibility = ''; });
  await wait(60);
  // 挑一家**没归并过、公司名非空**的公司那行（用它的按钮做归并）
  const b0 = btns().filter((b) => {
    const tr = b.closest('tr');
    if (tr.classList.contains('grp')) return false;
    const c = tr.querySelector('.c-name .cellv');
    return c && String(c.dataset.v || '').trim();
  })[0] || btns()[0];
  const tr0 = b0.closest('tr');
  // ⚠️ **真正写进接口的是按钮上的 `data-mg`**（那是归一后的公司键），不是格子里显示的完整名 ——
  //    两者可以不一样（带省市前缀的那种，归一后会短一截）。
  //    这里原来是从 `.cellv` 的 dataset.v 推的，她数据里排第一的那家一换名字就假红了
  //    （而且 `tr.sub` 的第一格根本没有 .cellv，只有 subLabel）。
  //    `data-mgname` 才是显示名，跟子行 .subco 里写的、以及按钮上的「拆出」用的是同一个值。
  out.childKey = String(b0.dataset.mg || '');
  out.childName = String(b0.dataset.mgname || '');
  out.subBeforeClick = subRows().length;
  const w0 = (window.__wbody || []).length;
  b0.click();
  for (let i = 0; i < 25 && !document.querySelector('#appTb input.cellin'); i++) await wait(120);
  const inp = document.querySelector('#appTb input.cellin');
  if (!inp) { out.popMissing = true; out.writes = []; return out; }
  const dl = document.getElementById(inp.getAttribute('list') || '');
  out.pop = {
    pos: getComputedStyle(inp.closest('.cellpop')).position,
    ph: inp.placeholder,
    val: inp.value,
    options: dl ? dl.children.length : -1,
    tableW: tW(),
  };
  // 点按钮只该冒出面板，**不能顺手把这家的岗位摊开**（踩过「绑两次 = 看着像没反应」）
  out.subAfterClick = subRows().length;
  // 填一个她表里没有的主公司名 → 回车
  inp.value = NEWCO;
  inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  inp.blur();
  await wait(1800);
  const rs = rows();
  const grpRows = rs.filter((r) => r.classList.contains('grp'));
  const findParent = (arr) => arr.filter((r) => r.textContent.indexOf(NEWCO) >= 0)[0] || null;
  const pRow = findParent(grpRows);
  out.merged = {
    parentFound: !!pRow,
    badge: pRow ? ((pRow.querySelector('.gn') || {}).textContent || '').trim() : '',
    shorts: pRow ? ((pRow.querySelector('.gpos') || {}).textContent || '').trim() : '',
    rowsAfter: rs.filter((r) => !r.classList.contains('sub')).length,
    childStillTop: rs.filter((r) => !r.classList.contains('sub') && (() => {
      const c = r.querySelector('.c-name .cellv');
      return c && String(c.dataset.v || '').trim() === out.childName;
    })()).length,
    dupTop: (() => {                     // 顶层不许出现同名两行
      const names = rs.filter((r) => !r.classList.contains('sub')).map((r) => {
        const g = r.querySelector('.gtoggle span:last-child');
        const c = r.querySelector('.c-name .cellv');
        return String(g ? g.textContent : (c ? c.dataset.v : '')).trim();
      }).filter(Boolean);
      const c = {}; names.forEach((n) => { c[n] = (c[n] || 0) + 1; });
      return Object.keys(c).filter((n) => c[n] > 1);
    })(),
    subsAfter: ovSubs(ovText()),
    kpiTotal: document.querySelector('#appKpi .kpi-card b').textContent.trim(),
    countText: document.getElementById('appCount').textContent.trim(),
    labels: [...new Set(btns().map((b) => b.textContent.trim()))],
    parentHasBtn: !!(pRow && pRow.querySelector('button.mg')),
  };
  // 摊开全部：**纯岗位子行不该有按钮** —— 那是「某家公司的某一条记录」，不是「一家公司」。
  // 归并组里的子公司子行才给（它代表一家公司，且那颗是「拆出」）。
  document.getElementById('appOpenAll').click();
  await wait(700);
  const allSub = rows().filter((r) => r.classList.contains('sub'));
  out.jobRows = allSub.filter((r) => !r.querySelector('.subco')).length;
  out.jobRowsWithBtn = allSub.filter((r) => !r.querySelector('.subco') && r.querySelector('button.mg')).length;
  out.subcoWithBtn = allSub.filter((r) => r.querySelector('.subco') && r.querySelector('button.mg')).length;
  document.getElementById('appOpenAll').click();      // 收回折叠态
  await wait(700);
  // 点开主公司那行 → 子公司子行（上面收过一次，DOM 重铺了，这行得重新找）
  const pRow2 = findParent(rows().filter((r) => r.classList.contains('grp')));
  if (pRow2) {
    pRow2.children[0].click();
    await wait(600);
    const rs2 = rows();
    const i = rs2.findIndex((r) => r.classList.contains('grp') && r.textContent.indexOf(NEWCO) >= 0);
    const subs = [];
    for (let j = i + 1; j < rs2.length && rs2[j].classList.contains('sub'); j++) {
      subs.push({ co: ((rs2[j].querySelector('.subco') || {}).textContent || '').trim(),
                  mg: ((rs2[j].querySelector('button.mg') || {}).textContent || '').trim() });
    }
    out.opened = subs;
  }
  // 拆回去：子行那颗「拆出」→ 面板里点「不并了」（清空输入框 = 什么都不改，故意的）
  const outBtn = btns().filter((b) => b.textContent.trim() === '拆出')[0];
  if (outBtn) {
    outBtn.click();
    for (let i = 0; i < 25 && !document.querySelector('#appTb .cellquick button'); i++) await wait(120);
    const qs = [...document.querySelectorAll('#appTb .cellquick button')];
    out.quickOnMerged = qs.map((x) => x.textContent.trim());
    const clear = qs.filter((x) => x.textContent.trim() === '不并了')[0];
    if (clear) { clear.click(); await wait(1800); }
  }
  const rs3 = rows();
  out.afterClear = {
    parentGone: !rs3.some((r) => r.textContent.indexOf(NEWCO) >= 0),
    rows: rs3.filter((r) => !r.classList.contains('sub')).length,
    subs: ovSubs(ovText()),
  };
  out.writes = (window.__wbody || []).slice(w0)
    .filter((x) => /\/api\/app-parent$/.test(x.u)).map((x) => x.body);
  return out;
}, NEWCO);

check('折叠态下每一行（一家公司 / 一个主公司）都有且只有一颗按钮，标签只能是「并入」或「拆出」',
  mg.btnCount === mg.rowsBefore && mg.rowsBefore > 0 &&
  mg.btnLabels.length > 0 && mg.btnLabels.every((l) => l === '并入' || l === '拆出'),
  JSON.stringify({ 行: mg.rowsBefore, 按钮: mg.btnCount, 标签: mg.btnLabels, 已归并: mg.subsBefore }));
check('按钮都在最右那格、竖着排，而且一个像素都没把「操作」列撑宽（藏起来再量一次对照）',
  mg.btnInOpCell === mg.btnCount && mg.btnDisplay.join() === 'block' &&
  mg.colShown.op === mg.colHidden.op && mg.colShown.t === mg.colHidden.t,
  JSON.stringify({ 在最右格: mg.btnInOpCell, display: mg.btnDisplay, 显示: mg.colShown, 隐藏: mg.colHidden }));
check('点「并入」只冒出面板，不顺手把这家的岗位摊开（表格宽度也没动）',
  !mg.popMissing && mg.pop.pos === 'absolute' && mg.subAfterClick === mg.subBeforeClick &&
  mg.pop.tableW === mg.colShown.t,
  JSON.stringify({ pop: mg.pop, 点前子行: mg.subBeforeClick, 点后子行: mg.subAfterClick }));
check('面板里预填的是「她点的那家现在并给谁」（没并过就是空的），候选是她表里现有的公司名，还能现打新的',
  !mg.popMissing && mg.pop.val === '' && mg.pop.options > 5 &&
  /主公司/.test(mg.pop.ph || ''),
  JSON.stringify(mg.pop));
check('填一个她表里没有的主公司名 → 顶层多出主公司那行、被并那家从顶层消失、顶层不出重名',
  !mg.merged.parentFound ? false :
  mg.merged.parentFound && mg.merged.badge.indexOf('家子公司') >= 0 &&
  mg.merged.childStillTop === 0 && mg.merged.dupTop.length === 0 &&
  mg.merged.rowsAfter === mg.rowsBefore &&
  mg.merged.shorts.indexOf(mg.childName) >= 0,
  JSON.stringify({ badge: mg.merged.badge, shorts: mg.merged.shorts, 被并那家还在顶层: mg.merged.childStillTop,
                   重名: mg.merged.dupTop, 行: mg.rowsBefore + '→' + mg.merged.rowsAfter, child: mg.childName }));
check('归并后两个口径都在：总数不变、概览「已归并子公司」+1、主公司那行自己也带按钮',
  mg.merged.subsAfter === mg.subsBefore + 1 &&
  mg.merged.kpiTotal === String(mg.rowsBefore) &&
  mg.merged.countText.indexOf('共 ' + mg.rowsBefore + ' 家') >= 0 &&
  mg.merged.parentHasBtn === true,
  JSON.stringify({ 已归并: mg.subsBefore + '→' + mg.merged.subsAfter, kpi: mg.merged.kpiTotal,
                   计数: mg.merged.countText, 主行有按钮: mg.merged.parentHasBtn, 标签: mg.merged.labels }));
check('摊开全部：纯岗位子行不给按钮（那是某家的一条记录，不是一家公司），子公司子行才有',
  mg.jobRows > 0 && mg.jobRowsWithBtn === 0,
  JSON.stringify({ 岗位子行: mg.jobRows, 其中带按钮: mg.jobRowsWithBtn, 子公司子行带按钮: mg.subcoWithBtn }));
check('点开主公司那行：子公司子行写着是哪一家、那颗按钮是「拆出」',
  !!mg.opened && mg.opened.length === 1 && mg.opened[0].co === mg.childName &&
  mg.opened[0].mg === '拆出',
  JSON.stringify({ opened: mg.opened, child: mg.childName }));
check('已归并的那颗按钮点开：预填的是现在并给谁、还多一颗「不并了」；点它拆干净、回到原样',
  !!mg.quickOnMerged && mg.quickOnMerged.join() === '不并了' &&
  mg.afterClear.parentGone === true && mg.afterClear.rows === mg.rowsBefore &&
  mg.afterClear.subs === mg.subsBefore,
  JSON.stringify({ quick: mg.quickOnMerged, afterClear: mg.afterClear, 原: mg.rowsBefore, 已归并: mg.subsBefore }));
check('并入 / 拆出各发一次 /api/app-parent，体里就只有 child + parent 两个字段（parent 空串 = 拆出来）',
  mg.writes.length === 2 &&
  Object.keys(JSON.parse(mg.writes[0])).sort().join() === 'child,parent' &&
  JSON.parse(mg.writes[0]).child === mg.childKey && mg.childKey.length > 0 &&
  JSON.parse(mg.writes[0]).parent === NEWCO &&
  JSON.parse(mg.writes[1]).parent === '',
  JSON.stringify({ writes: mg.writes, childKey: mg.childKey, childName: mg.childName }).slice(0, 240));
let parentWritesExpected = mg.writes.length;

// ── ⑩b 拖动并入：把一家公司那一行拖到另一家那一行上 ──────────────────────────────
// 她 2026-10-07 第二句：「并入方法太麻烦了，能不能拖动并入」。
// 点按钮得开面板、还得把主公司名打一遍（表里没有的名字只能手打）；拖一行到另一行上只动鼠标。
// 这里用**合成的 DragEvent** 走一遍完整拖放（dragstart → dragover → drop → dragend）——
// 页面里那套 handler 只认事件本身（不看 `dataTransfer` 里装了什么），所以合成事件跟真拖
// 走的是**同一条代码路径**。写请求照样被拦成假响应，一个字节都没落到她的数据上。
console.log('\n⑩b 拖动并入');
// 先等 10 秒轮询**刚过去**再开始拖：拖放序列里全是固定等待（约 8~9 秒），
// 中途一旦撞上轮询重建表格，抓在手里的源行 / 落点就变成游离节点 ——
// 事件照样派发到旧节点上，后面「落点描一圈 / 发请求 / 升成组头」一整串全假红。
// 轮询相位跟着整轮时长走，所以这类红是**成簇**出现的（2026-10-08 实测 3/3 命中）。
// 轮询一结束就开始，整个序列赶在下一次轮询之前跑完；万一监听不到，11 秒兜底放行。
await new Promise((resolve) => {
  const done = () => { clearTimeout(t); page.off('requestfinished', onReq); resolve(); };
  const t = setTimeout(done, 11000);
  const onReq = (req) => { if (/\/api\/board(\?|$)/.test(req.url())) done(); };
  page.on('requestfinished', onReq);
});
const dr = await page.evaluate(async () => {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const rows = () => [...document.querySelectorAll('#appTb tr')];
  const drags = () => [...document.querySelectorAll('#appTb tr[draggable="true"]')];
  const nm = (r) => r.dataset.drgName || '';
  const jobRows = () => rows().filter((r) => r.classList.contains('sub') && !r.querySelector('button.mg'));
  // 「还没归并过、自己也不是主公司」的行 —— 拿它们当拖动源 / 落点才干净
  const free = () => drags().filter((r) => {
    const b = r.querySelector('button.mg');
    if (!b || b.dataset.mgparent) return false;
    const gn = r.querySelector('.gn');
    return !gn || gn.textContent.indexOf('家子公司') < 0;
  });
  const ovSubs = () => {
    // 同上：那几个数 2026-10-10 起在 title 里，textContent 里没有了。
    const o = document.getElementById('appOverview');
    const m = (o.textContent + ' ' + (o.title || '')).replace(/\s+/g, ' ')
      .match(/已归并子公司\s*(\d+)\s*家/);
    return m ? +m[1] : 0;
  };
  const wN = () => (window.__wbody || []).length;
  const dt = new DataTransfer();
  const fire = (el, t) => el.dispatchEvent(new DragEvent(t,
    { bubbles: true, cancelable: true, dataTransfer: dt }));
  const out = {};
  // ⚠️ 折叠态下**每一行都代表一家公司**（多岗位公司就那么一行），压根看不到纯岗位子行 ——
  //    要量「岗位子行不能拖」得先把它们摊开。
  const oa = document.getElementById('appOpenAll');
  if (oa && !oa.hidden && oa.textContent.indexOf('展开全部') >= 0) { oa.click(); await wait(900); }
  out.dragRows = drags().length;
  out.plainRows = rows().length - drags().length;      // 岗位子行（一条记录，不是一家公司）
  out.freeRows = free().length;
  const src = free()[0], tgt = free()[1];
  if (!src || !tgt) { out.跳过 = '没有两家可并的'; return out; }
  out.src = nm(src); out.tgt = nm(tgt);
  // ⚠️ 写进接口的是按钮上的 `data-mg`（归一后的键），界面上显示的是 `data-mgname`。
  //    两个可以不一样，这里是按「写出去的那个」对账。
  out.srcKey = String(src.dataset.drgKey || '');
  out.源光标 = getComputedStyle(src).cursor;
  const jr0 = jobRows()[0];
  out.子行光标 = jr0 ? getComputedStyle(jr0).cursor : null;
  out.已归并Before = ovSubs();

  // ⚠️ 「落在自己身上」要单独走完一次完整序列 —— 真实拖放里 **drop 只会发生一次**，
  //    页面也在 drop 里就清掉了拖动状态；一次序列里连做两次 drop 的话，第二次必然是空操作
  //    （第一版就是这么写的，结果「真拖」那一下静默失效、白红 5 条）。
  const w0 = wN();
  fire(src, 'dragstart');
  out.拖起来_源行淡下去 = src.classList.contains('dragging');
  fire(src, 'dragover');
  out.自己身上_不描 = !src.classList.contains('dropok');
  fire(src, 'drop');
  fire(src, 'dragend');
  out.自己身上_不发请求 = (wN() - w0) === 0;
  out.松手后_高亮都收掉 = !src.classList.contains('dragging') && !src.classList.contains('dropok');
  await wait(400);

  // 二次定位：万一上面那段等待正好赶上轮询重建，手里的节点就游离了 —— 按名字重新抓一遍。
  // ⚠️ 抓不到（表还没渲染出来 / 这一行真没了）就**等一等再抓**，最多等 6 秒；
  //    绝不拿游离节点凑数 —— 游离节点上的事件冒泡不到 #appTb，dragstart 会被静默吞掉，
  //    后面 dragover/drop 全部空转，⑩b 连环假红还多发一次误并请求（2026-10-08 实测踩到：
  //    两次连跑都赶上重建，7 条一起红）。是「真没了」就如实报失败，别静默跳过。
  let src2 = null, tgt2 = null;
  for (let i = 0; i < 30 && !(src2 && tgt2); i++) {
    const s = drags().filter((r) => nm(r) === out.src)[0];
    const t = drags().filter((r) => nm(r) === out.tgt)[0];
    if (s && t && s !== t && s.isConnected && t.isConnected) { src2 = s; tgt2 = t; break; }
    await wait(200);
  }
  // 再来一次**真的**拖动
  let w1 = wN();
  if (src2 && tgt2) {
    w1 = wN();
    fire(src2, 'dragstart');
    out.真拖_源行接上了 = src2.classList.contains('dragging');   // 没接上 = dragstart 被吞，失败详情里一眼能看出来
    fire(tgt2, 'dragover');
    out.拖起来_落点描一圈 = tgt2.classList.contains('dropok');
    fire(tgt2, 'drop');
    fire(src2, 'dragend');
    out.松手后_落点高亮收掉 = !tgt2.classList.contains('dropok') && !src2.classList.contains('dragging');
  } else {
    out.真拖_源行接上了 = false;
    out.拖起来_落点描一圈 = false;
  }
  await wait(1800);
  const wr = (window.__wbody || []).slice(w1);
  out.writes = wr.filter((x) => /\/api\/app-parent$/.test(x.u)).map((x) => x.body);
  out.别的写请求 = wr.filter((x) => !/\/api\/app-parent$/.test(x.u)).map((x) => x.u);
  const s2 = drags().filter((r) => nm(r) === out.src)[0];
  const sb = s2 && s2.querySelector('button.mg');
  out.被拖那家_已归并 = !!(sb && sb.dataset.mgparent === out.tgt && sb.textContent.trim() === '拆出');
  out.被拖那家_还带按钮 = !!sb;
  const t2 = drags().filter((r) => nm(r) === out.tgt)[0];
  out.主行 = t2 ? {
    是组头: t2.classList.contains('grp'),
    徽标: (t2.querySelector('.gn') || {}).textContent || '',
    展开着: t2.classList.contains('open'),
    短名: (t2.querySelector('.gpos') || {}).textContent || '',
    按钮: ((t2.querySelector('button.mg') || {}).textContent || '').trim(),
  } : null;
  out.已归并After = ovSubs();
  out.计数 = document.getElementById('appCount').textContent.replace(/\s+/g, ' ');
  // 成环：把升成主公司的那行，拖回它自己的子公司上 → 拦住，而且**不发请求**
  if (t2 && s2) {
    const w1 = wN();
    fire(t2, 'dragstart'); fire(s2, 'drop'); fire(t2, 'dragend');
    await wait(900);
    out.成环_不发请求 = (wN() - w1) === 0;
    out.成环_主公司还在顶层 = !!drags().filter((r) => nm(r) === out.tgt)[0];
    out.成环_没被拖坏 = !!drags().filter((r) => nm(r) === out.src)[0];
  }
  // 从「并入 / 拆出」那颗按钮上按下，不算拖这一行（她编辑到一半别被拖走）
  const tm = drags().filter((r) => nm(r) === out.tgt)[0];
  if (tm) {
    const w2 = wN();
    fire(tm.querySelector('button.mg'), 'dragstart');
    out.从按钮按下_没进拖动态 = !tm.classList.contains('dragging');
    fire(tm, 'dragend');
    out.按钮按下_不发请求 = (wN() - w2) === 0;
  }
  out.岗位子行可拖 = jr0 ? jr0.draggable : null;
  // 拖动那会儿撞上 10 秒一次的轮询：表格绝不能被重建（一重建这次拖动作废）
  const sr = drags()[0], first = rows()[0];
  fire(sr, 'dragstart');
  if (typeof renderApps === 'function') renderApps();
  out.拖动中_表格没被重建 = rows()[0] === first;
  fire(sr, 'dragend');
  if (typeof renderApps === 'function') renderApps();
  await wait(400);
  out.松开后_能重建 = rows()[0] !== first;
  // 收尾：把这次拖出来的归并拆回去（点按钮 → 「不并了」），别给后面的用例留半成品 ——
  // 顺带证明「拖出来的那对，老路子照样拆得掉」。
  const s3 = drags().filter((r) => nm(r) === out.src)[0];
  if (s3 && s3.querySelector('button.mg')) {
    const w3 = wN();
    s3.querySelector('button.mg').click();
    for (let i = 0; i < 25 && !document.querySelector('#appTb input.cellin'); i++) await wait(120);
    const qi = [...document.querySelectorAll('#appTb .cellquick button')]
      .filter((x) => x.textContent.trim() === '不并了')[0];
    if (qi) { qi.click(); await wait(1600); }
    // ⚠️ 只能查「**这一次拖出来的那对**拆干净了没」—— 不许查「整张表里一行动作都没有」：
    //    内置打底的 `TCL实业 → TCL` 永远在表里（她真实数据天生就命中），那样查必然假红。
    const s4 = drags().filter((r) => nm(r) === out.src)[0];
    const b4 = s4 && s4.querySelector('button.mg');
    out.拆得掉 = (wN() - w3) === 1 && !!qi && !!(b4 && !b4.dataset.mgparent);
    out.已归并Back = ovSubs();
  }
  return out;
});
check('拖动并入：能拖的就是「一家一行」的那些行（含主公司那行），岗位子行一律不能拖',
  dr.dragRows > 0 && dr.freeRows >= 2 && dr.plainRows > 0 &&
  dr.岗位子行可拖 === false && dr.源光标 === 'grab' && dr.子行光标 !== 'grab',
  JSON.stringify({ 可拖: dr.dragRows, 不可拖: dr.plainRows, 可并的: dr.freeRows,
                   光标: [dr.源光标, dr.子行光标], 岗位子行可拖: dr.岗位子行可拖 }));
check('拖动时：源行淡下去、落点描一圈；拖到自己身上就不描了，松手也确实什么都不发生',
  dr.拖起来_源行淡下去 === true && dr.拖起来_落点描一圈 === true &&
  dr.自己身上_不描 === true && dr.自己身上_不发请求 === true &&
  dr.松手后_高亮都收掉 === true && dr.松手后_落点高亮收掉 === true,
  JSON.stringify({ 源行淡: dr.拖起来_源行淡下去, 落点描: dr.拖起来_落点描一圈,
                   自己身上: dr.自己身上_不描, 自己身上没发请求: dr.自己身上_不发请求,
                   松手收干净: [dr.松手后_高亮都收掉, dr.松手后_落点高亮收掉] }));
check('拖一家到另一家上：只发一次 /api/app-parent（体里就 child + parent），被拖那家从顶层消失',
  (dr.writes || []).length === 1 &&
  Object.keys(JSON.parse(dr.writes[0])).sort().join() === 'child,parent' &&
  JSON.parse(dr.writes[0]).child === dr.srcKey && dr.srcKey.length > 0 &&
  JSON.parse(dr.writes[0]).parent === dr.tgt &&
  (dr.别的写请求 || []).length === 0 &&
  dr.被拖那家_已归并 === true && dr.被拖那家_还带按钮 === true,
  JSON.stringify({ writes: dr.writes, 其他写: dr.别的写请求, srcKey: dr.srcKey,
                   已归并: dr.被拖那家_已归并 }));
check('拖完落点那家升成主公司行：是组头、徽标写「N 家子公司 · N 个岗位」、自动摊开、自己还带按钮',
  !!dr.主行 && dr.主行.是组头 === true && dr.主行.徽标.indexOf('家子公司') >= 0 &&
  dr.主行.展开着 === true && dr.主行.短名.indexOf(dr.src) >= 0 && dr.主行.按钮 === '并入',
  JSON.stringify(dr.主行));
check('拖完两个口径都对：概览「已归并子公司」+1、右下角计数不变（家数少 1、岗位数不动）',
  dr.已归并After === dr.已归并Before + 1 &&
  dr.计数.indexOf('共 ') >= 0 && dr.已归并Back === dr.已归并Before,
  JSON.stringify({ 已归并: dr.已归并Before + '→' + dr.已归并After + '→' + dr.已归并Back,
                   计数: dr.计数 }));
check('拖动成环要拦住：把主公司那行拖回它自己的子公司上 —— 拦住、不发请求、两边都没被拖坏',
  dr.成环_不发请求 === true && dr.成环_主公司还在顶层 === true && dr.成环_没被拖坏 === true,
  JSON.stringify({ 没发请求: dr.成环_不发请求, 主公司还在顶层: dr.成环_主公司还在顶层,
                   子公司也还在: dr.成环_没被拖坏 }));
check('从「并入 / 拆出」那颗按钮上按下，不算拖这一行（她编辑到一半不会被拖走）',
  dr.从按钮按下_没进拖动态 === true && dr.按钮按下_不发请求 === true, JSON.stringify(dr));
check('拖动那会儿 10 秒一次的轮询不许重建表格（一重建这次拖动作废），松手后照常重建',
  dr.拖动中_表格没被重建 === true && dr.松开后_能重建 === true,
  JSON.stringify({ 拖动中没重建: dr.拖动中_表格没被重建, 松开后能重建: dr.松开后_能重建 }));
check('拖动并出来的那对，老路子照样拆得掉（点按钮 →「不并了」，只发一次 parent 空串的请求）',
  dr.拆得掉 === true, JSON.stringify({ 拆得掉: dr.拆得掉 }));
parentWritesExpected += (dr.writes || []).length + (dr.拆得掉 ? 1 : 0);

// 她那台机器只有 1080px —— 多了一列按钮之后最容易在这儿横向撑破
for (const w of [1080, 980]) {
  await page.setViewport({ width: w, height: 900 });
  await sleep(400);
  const r = await page.evaluate(() => {
    const tb = document.getElementById('appTb');
    const bar = document.querySelector('#pane-apps .bar');
    return { sw: document.documentElement.scrollWidth, vw: window.innerWidth,
             barOver: bar ? bar.scrollWidth - bar.clientWidth : 0,
             tbOver: tb ? tb.scrollWidth - tb.clientWidth : 0 };
  });
  check(`${w}px：投递进度表加了「并入」按钮也不横向溢出（表 / 工具条 / 页面都不出滚动条）`,
    r.sw <= r.vw && r.barOver <= 1 && r.tbOver <= 1, JSON.stringify(r));
}
await page.setViewport({ width: 1080, height: 900 });

// ---------- ⑪ 通知页「手动添加 / 删除」（v0.35.0）----------
// 有些面试 / 测评是短信或电话来的，邮箱里没有，她要自己补一条。这一组全部走**假响应**：
// 保存 / 删除的 POST 都被上面的拦截桩挡下并回假 ok，一个字节都没落到她的 notices_manual.json 上。
console.log('\n⑪ 通知页手动添加 / 删除');
await fresh('notices', 'at.ntv.v1');
await page.evaluate(() => document.getElementById('ntvDl').click());
await sleep(500);

// ① 点「＋ 手动添加」→ 面板从隐藏变出来，字段默认值正确
const addOpen = await page.evaluate(() => {
  const box = document.getElementById('ntAddBox');
  const before = box.hidden;
  document.getElementById('ntAdd').click();
  const s = document.getElementById('ntAddType');
  const dl = document.getElementById('ntAddDl');
  const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(23, 59, 0, 0);
  const p = (n) => String(n).padStart(2, '0');
  const want = d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T23:59';
  return {
    beforeHidden: before, afterHidden: box.hidden,
    hasCo: !!document.getElementById('ntAddCo'),
    hasDatalist: !!document.getElementById('ntCoList'),
    type: s ? s.value : '(缺)', dl: dl ? dl.value : '(缺)', wantDl: want,
    quick: document.querySelectorAll('#ntAddBox button.quick').length,
    noteEmpty: (document.getElementById('ntAddNote') || {}).value === '',
  };
});
check('点「＋ 手动添加」→ 面板从隐藏变出来，公司名 / 日期控件都在',
  addOpen.beforeHidden === true && addOpen.afterHidden === false &&
  addOpen.hasCo && addOpen.hasDatalist, JSON.stringify(addOpen));
check('表单默认值对：类型预选「面试」、截止预填「明天 23:59」、三颗快捷键、说明留空不预填',
  addOpen.type === '面试' && addOpen.dl === addOpen.wantDl && addOpen.quick === 3 && addOpen.noteEmpty,
  JSON.stringify(addOpen));

// ② 公司名留空点保存 → 一个请求都不发，只出一句提示，面板不关
const addEmpty = await page.evaluate(async () => {
  const before = (window.__wbody || []).length;
  document.getElementById('ntAddCo').value = '';
  document.getElementById('ntAddSave').click();
  await new Promise((r) => setTimeout(r, 500));
  return { writes: (window.__wbody || []).length - before,
           toast: (document.getElementById('toast') || {}).textContent || '',
           boxHidden: document.getElementById('ntAddBox').hidden };
});
check('公司名空着点保存 → 一个请求都不发，只出一句提示，面板不关（输入留着）',
  addEmpty.writes === 0 && /先填公司名/.test(addEmpty.toast) && addEmpty.boxHidden === false,
  JSON.stringify(addEmpty));

// ③ 填公司名点保存 → 请求体字段 / 格式正好对（截止时间**空格**分隔、类型「面试」）
const addSave = await page.evaluate(async () => {
  const before = (window.__wbody || []).length;
  document.getElementById('ntAddCo').value = '示例公司';
  document.getElementById('ntAddPos').value = '后端开发';
  document.getElementById('ntAddType').value = '面试';
  document.getElementById('ntAddDl').value = '2026-10-12T23:59';
  document.getElementById('ntAddLink').value = 'hr.example.com/t/abc';
  document.getElementById('ntAddNote').value = '短信通知的';
  document.getElementById('ntAddSave').click();
  await new Promise((r) => setTimeout(r, 800));
  const w = (window.__wbody || []).slice(before).filter((x) => /\/api\/notice-add$/.test(x.u));
  let body = {};
  try { body = JSON.parse((w[0] && w[0].body) || '{}'); } catch (e) { body = {}; }
  return { n: w.length, body: body,
           boxHidden: document.getElementById('ntAddBox').hidden,
           toast: (document.getElementById('toast') || {}).textContent || '' };
});
check('填好点保存 → 只发一次 /api/notice-add，请求体字段与格式正好对（截止时间「空格」分隔、类型「面试」）',
  addSave.n === 1 && addSave.body['公司名称'] === '示例公司' && addSave.body['类型'] === '面试' &&
  addSave.body['截止时间'] === '2026-10-12 23:59' && addSave.body['岗位名称'] === '后端开发' &&
  addSave.body['链接'] === 'hr.example.com/t/abc' && addSave.body['说明'] === '短信通知的',
  JSON.stringify(addSave));
check('保存成功后关面板、清空输入，并出「已添加」提示',
  addSave.boxHidden === true && addSave.toast.indexOf('已添加') >= 0, JSON.stringify(addSave));

// ④ 手动行渲染 + 删除入口：塞一条 来源=手动 的行，点两下才发一次删除
// ⚠️ 只认**这一条注入行**，绝不数全表 —— 她本人 2026-10-11 16:15 起已经在用这个功能
// （真机上 notices_manual.json 里有 2 条），全表写死「手动标签 === 1」会当场变假红；
// 同样地，`querySelector('#ntTb button.ntdel')` 取到的那颗很可能正是她那条真实通知，
// 点下去虽然被桩挡着不落盘，但后面的 id 断言会错。所以按公司名缩到那一格里去取。
const del = await page.evaluate(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const CO = '手动示例公司';
  const cntTags = () => [...document.querySelectorAll('#ntTb td.c-name .tag')]
    .filter((e) => e.textContent.trim() === '手动').length;
  const tagBefore = cntTags();
  NOTICES = NOTICES.concat([{
    公司名称: CO, 岗位名称: '', 通知类型: ['面试'],
    通知截止时间: '2026-10-12 23:59', 邮件链接: '', 邮件主题: '短信通知的',
    邮件收到时间: '2026-10-11 13:20', 处理状态: '', 原始处理状态: '', 状态来源: '',
    _key: 'manual-key-1', _gkey: CO, 来源: '手动', _mid: 'abc12345',
  }]);
  renderNotices();
  await sleep(300);
  const cell = [...document.querySelectorAll('#ntTb td.c-name')]
    .find((td) => td.textContent.trim().indexOf(CO) === 0 && td.querySelector('.tag'));
  const tagN = cntTags() - tagBefore;      // 注入使它 +1，跟表里有几条真实手动行无关
  const btn = cell ? cell.querySelector('button.ntdel') : null;
  const out = { tagN: tagN, hasBtn: !!btn, extraCol: document.querySelectorAll('#pane-notices thead th').length };
  if (!btn) return out;
  const before = (window.__wbody || []).length;
  btn.click();                        // 第一下：只变「确认删」，不发请求
  await sleep(180);
  out.afterFirst = btn.textContent.trim();
  out.afterFirstWrites = (window.__wbody || []).length - before;
  btn.click();                        // 第二下：真发
  await sleep(700);
  const w = (window.__wbody || []).slice(before).filter((x) => /\/api\/notice-del$/.test(x.u));
  out.delWrites = w.length;
  try { out.delBody = JSON.parse((w[0] && w[0].body) || '{}'); } catch (e) { out.delBody = {}; }
  return out;
});
check('手动行带「手动」标签 + 删除入口（这一条注入行 +1，也没给表加列 —— 表头还是 9 列）',
  del.tagN === 1 && del.hasBtn && del.extraCol === 9, JSON.stringify(del));
check('点一下只变「确认删」、不发请求；再点一下才发，而且只发一次 /api/notice-del（id 正好是这条）',
  del.afterFirst === '确认删' && del.afterFirstWrites === 0 && del.delWrites === 1 &&
  del.delBody.id === 'abc12345', JSON.stringify(del));
const noticeAddExpected = addSave.n;
const noticeDelExpected = del.delWrites;

await drainWrites();
const blocked = ALL_BLOCKED.slice();
// 页面加载时自己会 POST 一次 /api/apps-sync（后台顺手同步），那是原有行为、不是体检点出来的；
// ⑥ 里点灰块 / 回退环节也会 POST /api/interview —— 那是**故意**点的，且上面已单独断言只发到那个口。
// /api/status 同理：④ 的「整行可改」故意点了 4 下；/api/app-parent 是 ⑩ 故意点的两下。
// 所以这里不是把它放行，而是**数着来** —— 多一次就说明别处偷偷改了她的记录。
// ⑥c 的 /api/mail-source、/api/mail-off 同理：故意点的那两下（而且只在真有那两颗按钮时才点）。
const recordWrites = blocked.filter((b) => !/\/api\/(apps-sync|interview|app-parent)$/.test(b) &&
                                            !/\/api\/status$/.test(b) &&
                                            !/\/api\/mail-(source|off|on)$/.test(b) &&
                                            !/\/api\/notice-(add|del)$/.test(b));
check('全程没有发出任何记账类写请求（除了故意点的同步 / 进度 / 整行可改 / 并入 / 邮箱连接状态 / 手动通知）',
  recordWrites.length === 0, JSON.stringify(recordWrites).slice(0, 160));
check('全程写 /api/mail-source 的次数正好等于 ⑥c 点「改用直连收信」的次数（别处没偷换她的数据源）',
  blocked.filter((b) => /\/api\/mail-source$/.test(b)).length === mailSrcExpected,
  '实际 ' + blocked.filter((b) => /\/api\/mail-source$/.test(b)).length + ' / 预期 ' + mailSrcExpected);
check('全程写 /api/mail-off 的次数正好等于 ⑥c 点「断开」的次数（别处没偷关她的邮箱）',
  blocked.filter((b) => /\/api\/mail-off$/.test(b)).length === mailOffExpected,
  '实际 ' + blocked.filter((b) => /\/api\/mail-off$/.test(b)).length + ' / 预期 ' + mailOffExpected);
check('全程写 /api/app-parent 的次数正好等于 ⑩ 里点出来的次数（别处没偷改归并表）',
  blocked.filter((b) => /\/api\/app-parent$/.test(b)).length === parentWritesExpected,
  '实际 ' + blocked.filter((b) => /\/api\/app-parent$/.test(b)).length + ' / 预期 ' + parentWritesExpected);
check('全程写 /api/status 的次数正好等于「整行可改」那条用例点出来的次数（别处没偷改）',
  blocked.filter((b) => /\/api\/status$/.test(b)).length === statusWritesExpected,
  '实际 ' + blocked.filter((b) => /\/api\/status$/.test(b)).length + ' / 预期 ' + statusWritesExpected);
check('全程写 /api/notice-add 的次数正好等于⑪里点保存的次数（别处没偷加手动通知）',
  blocked.filter((b) => /\/api\/notice-add$/.test(b)).length === noticeAddExpected,
  '实际 ' + blocked.filter((b) => /\/api\/notice-add$/.test(b)).length + ' / 预期 ' + noticeAddExpected);
check('全程写 /api/notice-del 的次数正好等于⑪里点两下删除发出去的次数（别处没偷删）',
  blocked.filter((b) => /\/api\/notice-del$/.test(b)).length === noticeDelExpected,
  '实际 ' + blocked.filter((b) => /\/api\/notice-del$/.test(b)).length + ' / 预期 ' + noticeDelExpected);
check('全程无 JS 报错', errs.length === 0, [...new Set(errs)].join(' | ').slice(0, 200));
// ⚠️ 最后一道闸（见文件头上 CFG_BEFORE 那段）：跑完再按一次 config.ini 指纹。
// 这一条跟上面那些「请求次数对账」是两层保险 —— 对账看的是**页面拦截层**记下的账，
// 只要有一处绕过了桩（换页 / 旧标签页 / 将来重构），账本照样是干净的，而文件其实已经被改了。
check('全程没动过她的 config.ini（首尾指纹一致 —— 体检绝不许改授权码 / 数据源）',
  configFingerprint() === CFG_BEFORE,
  CFG_BEFORE + ' → ' + configFingerprint() + '（' + CONFIG_FILE + '）');

console.log(`\n########## 通过 ${pass} 项，失败 ${fails.length} 项 ##########`);
if (fails.length) { fails.forEach((f) => console.log('  ✗ ' + f)); }

}

// 跑崩也要把页面关掉。以前 main() 里抛错就直接 `process.exit(1)`，
// 上面那段收尾（page.close / browser.disconnect）根本没机会执行，
// 于是**残留的标签页会留到下一轮**：它每 10 秒轮询一次看板，还会撞上页面那条
// 「版本对不上就自动刷新一次」的逻辑 —— 下一轮的断言就变成时红时不红。
// 2026-10-08 实测踩到过：演示服务没起来那次崩在 fresh() 里，
// 等下一次正常跑完之后，它开的那个页面还挂在 CDP 的 /json/list 里没关。
// 所以正常跑完、跑崩，都从这里出去，走同一条收尾。
async function shutdown(code) {
  try { if (PAGE && !PAGE.isClosed()) await PAGE.close(); } catch (e) { /* 已经关了 / 浏览器没了 */ }
  try { if (BROWSER) BROWSER.disconnect(); } catch (e) { /* 连接已经断了 */ }
  process.exit(code);
}

main().then(() => shutdown(fails.length ? 1 : 0),
            (e) => { console.error(e); shutdown(1); });
