// 给 samples/ 出 README 用的截图（视口宽 1100px —— 就是日常窗口宽，别按桌面宽校验）。
//
// 前置：一个在跑的看板服务 + 一个可连的浏览器调试端口。
//   1) 起服务（想用脱敏演示数据的话，先 python3 tools/make-demo-data.py 再在那边 --serve）
//   2) ~/.workbuddy/bin/safe-browser serve        # 或任何 `--remote-debugging-port` 的 Chrome/Edge
//   3) BROWSER_URL=http://127.0.0.1:<port> BASE=http://127.0.0.1:8788 \
//        node tools/make-screenshots.js
//
// 依赖 puppeteer-core（不下载浏览器，只连现成的）：
//   npm i puppeteer-core  或  PUPPETEER_DIR=/path/to/node_modules node tools/make-screenshots.js
//
// 注意：截图里会出现数据。**给开源仓库用的图必须拿演示数据拍**
// （tools/make-demo-data.py 生成），别对着真实数据按快门。

const fs = require('fs');
const path = require('path');

const BASE = process.env.BASE || 'http://127.0.0.1:8788';
const SAMPLES = process.env.SAMPLES || path.join(__dirname, '..', 'samples');
const W = 1100;

const puppeteer = (() => {
  const dirs = [
    process.env.PUPPETEER_DIR,
    process.env.HOME && path.join(process.env.HOME, '.workbuddy/binaries/node/workspace/node_modules'),
  ].filter(Boolean);
  for (const d of dirs) {
    try { return require(path.join(d, 'puppeteer-core')); } catch (e) { /* 继续找 */ }
  }
  try { return require('puppeteer-core'); } catch (e) {
    console.error('找不到 puppeteer-core。先 npm i puppeteer-core，'
      + '或用 PUPPETEER_DIR 指向它所在的 node_modules 目录。');
    process.exit(1);
  }
})();

// 同步提示的文案在页面上直接显示，演示环境里没有飞书可同步，
// 所以在页面加载前把 fetch 换掉：/api/apps-sync 假装成功，
// /api/board 的 meta 里补上同步结果，这样截出来的是「同步成功」的样子。
const FAKE_SYNC = `
(() => {
  const real = window.fetch.bind(window);
  const NOW = '09-30 19:36';
  window.fetch = async (...args) => {
    const url = String(args[0] && args[0].url ? args[0].url : args[0]);
    if (url.indexOf('/api/apps-sync') >= 0) {
      return new Response(JSON.stringify({ ok: true }), {
        status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    const res = await real(...args);
    if (url.indexOf('/api/board') >= 0) {
      const d = await res.clone().json().catch(() => null);
      if (d && d.meta) {
        d.meta.apps_state = '已同步 44 条（新增 2 / 更新 1 / 待投递 9）';
        d.meta.apps_bad = false;
        d.meta.apps_busy = false;
        d.meta.apps_sync_at = NOW;
        // 按钮只在「投递记录来自飞书」时才显示（本地 CSV 模式下点了也没用，会藏起来）。
        // 演示数据是本地文件，这里把来源标成 feishu，截图里才看得到这个按钮。
        d.meta.apps_source = 'feishu';
        return new Response(JSON.stringify(d), {
          status: 200, headers: { 'Content-Type': 'application/json' } });
      }
    }
    return res;
  };
})();
`;

const SHOTS = [
  // 投递进度：上面有 KPI 卡 + 三块统计，矮了表体一行都看不到
  // group: 先把「同公司多岗位」摊开 —— 截图里要能同时看到公司主行、岗位子行，
  // 以及按**公司**数算的那几个 KPI（一家投几个岗位只算一个「投递」）。
  { file: 'shot-apply.png',        hash: '#apps',    h: 1080, scroll: 0, group: true },
  // 邮箱页现在是**两个可切换的视图**（v0.22.0），所以这一页的图分两组拍，
  // 都用元素定位而不是写死像素 —— 演示数据多一家公司就拍歪，而且看不出来。
  //   shot-notices.png      = 「面试测评截止」那一版（切换条 + 三个数字 + 一块分布 + 筛选栏）
  //   shot-notices-rows.png = 同一版往下滚：表头 + 表体（看得见「已完成」那几行的距截止是破折号）
  //   shot-interview.png    = 切到「投递进度」那一版（进度条只在这一版里）
  //   shot-notices-dark.png = 通知表那一版，深色
  { file: 'shot-notices.png',      hash: '#notices', h: 700,  scroll: 0 },
  { file: 'shot-notices-rows.png', hash: '#notices', h: 660,  to: '#ntTb', off: -130 },
  { file: 'shot-interview.png',    hash: '#notices', h: 780,  view: 'iv', scroll: 0 },
  { file: 'shot-notices-dark.png', hash: '#notices', h: 660,  dark: true, to: '#ntTb', off: -130 },
  // 机会库工具栏有三行（包含 / 不含 / 筛选控件），矮了看不到表体，单独给高一点
  { file: 'shot-opps.png',         hash: '#opps',    h: 1150, scroll: 0, opps: true },
];

const SYNC_FILE = 'shot-apps-sync.png';

function wait(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function newPage(browser, { h, dark }) {
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
  await page.setViewport({ width: W, height: h, deviceScaleFactor: 2 });
  await page.emulateMediaFeatures([
    { name: 'prefers-color-scheme', value: dark ? 'dark' : 'light' },
  ]);
  return { page, errs };
}

async function gotoBoard(page, hash) {
  await page.goto('about:blank', { waitUntil: 'load' });
  await page.goto(BASE + '/' + (hash || ''), { waitUntil: 'domcontentloaded', timeout: 45000 });
  // 筛选条件会被 localStorage 记住，不清掉的话每跑一次条件就多叠一层
  await page.evaluate(() => { try { localStorage.clear(); } catch (e) {} });
}

// ---- 防呆：截图必须对着**脱敏演示数据**拍 ----
// 2026-10-02 一天踩了两次，都是同一类错：
//   ① config.ini 里的相对路径按「脚本所在目录」解析，光 cd 到演示目录起服务没用，得配 --base；
//   ② 给了 --base 但当前目录也有 config.ini（仓库那份带真实飞书 token），演示服务于是照
//      真实 token 把真实投递记录同步进了演示目录。两次都让真实公司名进过 samples/ 截图
//      （都没提交，发现后已还原）。
// 所以判据从「页面上至少有**一个**演示公司」升级成「页面上的公司名**全都**得是演示公司」——
// 原来那条太松：真实数据混进来时，演示数据那几行还留在后面，照样能通过。
// 名单来自演示目录里那份 CSV（DEMO_DIR）；没给就退回仓库里的 12 行样例，宁可误报中止也不放行。
function readCsvColumn(p, col) {
  if (!fs.existsSync(p)) return [];
  const txt = fs.readFileSync(p, 'utf8').replace(/^\uFEFF/, '');
  const lines = txt.split(/\r?\n/).filter(Boolean);
  if (lines.length < 2) return [];
  const i = lines[0].split(',').indexOf(col);
  if (i < 0) return [];
  // 演示数据的公司名里没有逗号 / 引号，直接切列就够
  return lines.slice(1).map((l) => (l.split(',')[i] || '').trim()).filter(Boolean);
}
function demoCompanyNames() {
  const want = [];
  if (process.env.DEMO_DIR)
    want.push(path.join(process.env.DEMO_DIR, 'data', 'applications.csv'),
              path.join(process.env.DEMO_DIR, 'data', 'notices.csv'));
  want.push(path.join(SAMPLES, '投递记录样例.csv'));
  const out = new Set();
  for (const f of want) readCsvColumn(f, '公司名称').forEach((n) => out.add(n));
  return { names: [...out], scoped: !!process.env.DEMO_DIR };
}

async function assertDemoData(browser) {
  const { names, scoped } = demoCompanyNames();
  if (!names.length) {
    console.error('✗ 读不到演示数据名单，没法确认数据脱敏了 —— 已中止，不写任何文件。');
    process.exit(1);
  }
  if (!scoped)
    console.log('· 没给 DEMO_DIR，只能拿仓库里那份 12 行样例当名单'
      + '（演示数据里的公司比它多就会中止，属正常误报）');
  const allowed = new Set(names);
  const { page } = await newPage(browser, { h: 900 });
  await gotoBoard(page, '#apps');
  await wait(1800);
  // 直接读页面里那两份数据（投递记录 / 通知），比肉眼扫文本准 ——
  // 页面上有多少家、分别叫什么，一个不漏。读不到就中止（宁可不出图，不能拍错）。
  const seen = await page.evaluate(() => {
    const pick = (arr) => (Array.isArray(arr)
      ? arr.map((r) => String((r && r['公司名称']) || '')).filter(Boolean) : null);
    try {
      return { apps: pick(APPS), nt: pick(NOTICES) };
    } catch (e) { return { err: String(e).slice(0, 120) }; }
  });
  // 这一页**不能关**：safe-browser 起的 Edge 没有自带空白标签页，关掉这最后一个
  // 标签页等于让整个浏览器退出，后面第一张图的 newPage 就会报 "Target closed"
  // （2026-10-02 连踩两次，不是偶发）。留着它，最后 disconnect 收尾即可。
  if (seen.err || !seen.apps) {
    console.error('✗ 读不到页面上的数据（' + (seen.err || '页面里的投递记录不是数组') + '）'
      + ' —— 没法确认数据脱敏了，已中止，一个文件都没写。');
    process.exit(1);
  }
  const all = [...seen.apps, ...(seen.nt || [])];
  const bad = [...new Set(all.filter((n) => !allowed.has(n)))];
  if (bad.length) {
    console.error('✗ 页面上有 ' + bad.length + ' 家公司不在演示名单里（例如 ' + bad.slice(0, 3).join('、')
      + '）—— 这是真实数据被读进来了。\n  检查服务是不是带上了 --base <演示目录> --config <演示目录>/config.ini。'
      + '\n  已中止，一个文件都没写。');
    process.exit(1);
  }
  const hits = [...allowed].filter((n) => all.includes(n));
  if (!hits.length) {
    console.error('✗ 看板上一个演示公司都没有 —— 这份数据不是 tools/make-demo-data.py 生成的。'
      + '\n  已中止，一个文件都没写。');
    process.exit(1);
  }
  console.log('· 数据校验通过：页面上 ' + new Set(all).size + ' 家公司全部来自演示数据（命中 '
    + hits.length + ' 家虚构企业），投递 ' + seen.apps.length + ' 条 / 通知 '
    + (seen.nt || []).length + ' 条');
}

(async () => {
  fs.mkdirSync(SAMPLES, { recursive: true });
  const browser = await puppeteer.connect({ browserURL: process.env.BROWSER_URL });
  await assertDemoData(browser);
  const report = [];

  for (const s of SHOTS) {
    const { page, errs } = await newPage(browser, s);
    await gotoBoard(page, s.hash);
    // 深色那张：把「深色」这一档显式写进浏览器再刷一次，而不是只靠系统偏好模拟 ——
    // 这样截图里标签栏右端点亮的正是「深色」，跟手动切过去的真实样子一致。
    if (s.dark) {
      await page.evaluate(() => {
        try { localStorage.setItem('at.theme.v1', JSON.stringify({ mode: 'dark' })); } catch (e) {}
      });
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 45000 });
    }
    // 邮箱页有两个视图（v0.22.0）：进度条在第二个里。跟 dark 一样走「写进浏览器再刷一次」，
    // 比去点那个按钮稳（点了还要等重排，拍出来可能是半张）
    if (s.view === 'iv') {
      await page.evaluate(() => {
        try { localStorage.setItem('at.ntv.v1', JSON.stringify('iv')); } catch (e) {}
      });
      await page.reload({ waitUntil: 'domcontentloaded', timeout: 45000 });
    }
    if (s.opps) await page.reload({ waitUntil: 'domcontentloaded', timeout: 45000 });
    await wait(2000);

    try {
      await page.waitForFunction(() => {
        const tbs = [...document.querySelectorAll('table tbody')];
        return tbs.some((t) => t.children.length > 0);
      }, { timeout: 25000 });
    } catch (e) { /* 空的也照样拍，报告里会写行数 0 */ }

    if (s.opps) {
      // 让「筛选正在用」看得见：挑数据里条数最多的那个行业，从下拉里选上。
      // 值一律从数据里算出来 —— 写死「互联网」这种词，换一套数据就筛成空表了，
      // 而空值的条件行会把全部行都过滤掉（实测），所以也不去动「包含 / 不含」那些条件行。
      const bestInd = await page.evaluate(async () => {
        try {
          const d = await (await fetch('/api/opps?limit=500')).json();
          const cnt = {};
          (d.rows || []).forEach((r) => (r.industries || []).forEach((i) => {
            cnt[i] = (cnt[i] || 0) + 1;
          }));
          const best = Object.entries(cnt).sort((a, b) => b[1] - a[1])[0];
          return best ? best[0] : '';
        } catch (e) { return ''; }
      });
      const applied = await page.evaluate((ind) => {
        const sel = document.getElementById('oppInd');
        if (!sel || !ind) return false;
        if (![...sel.options].some((o) => o.value === ind)) return false;
        sel.value = ind;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }, bestInd);
      await wait(2500);
      if (!applied) console.error('机会库：没能选中行业下拉，截图里会是未筛选的样子');
    }

    if (s.group) {
      await page.evaluate(() => {
        const b = document.getElementById('appOpenAll');
        if (b && !b.hidden && b.textContent.trim() === '展开全部') b.click();
      });
      await wait(700);
    }

    // 取景统一走「滚到某个元素」，不写死像素：演示数据一变，写死的偏移就拍歪了。
    if (s.to) {
      const ok = await page.evaluate((sel, off) => {
        const e = document.querySelector(sel);
        if (!e) return false;
        window.scrollTo(0, Math.max(0, e.getBoundingClientRect().top + window.scrollY + off));
        return true;
      }, s.to, s.off || 0);
      await wait(500);
      if (!ok) console.error('邮箱页：没找到 ' + s.to + '，' + s.file + ' 可能拍歪了');
    } else if (s.scroll) await page.evaluate((y) => window.scrollTo(0, y), s.scroll);
    else await page.evaluate(() => window.scrollTo(0, 0));
    await wait(400);

    const out = path.join(SAMPLES, s.file);
    await page.screenshot({ path: out, fullPage: false });

    const info = await page.evaluate(() => {
      const de = document.documentElement;
      const pane = document.querySelector('.pane.on');
      return {
        行数: pane ? pane.querySelectorAll('tbody tr').length : -1,
        横向溢出: de.scrollWidth - de.clientWidth,
        页码高: document.documentElement.scrollHeight,
      };
    });
    report.push({ 文件: s.file, 视口: W + 'x' + s.h, ...info, 报错: errs });
    await page.close();
  }

  // ---- 「同步飞书进度」按钮 + 结果提示 ----
  {
    const { page, errs } = await newPage(browser, { h: 760 });
    await page.evaluateOnNewDocument(FAKE_SYNC);
    await gotoBoard(page, '#apps');
    await page.waitForFunction(() => document.querySelectorAll('#appTb tr').length > 0,
                               { timeout: 20000 });
    await wait(800);

    // 让工具栏那一条连同上面几行一起进画面
    const y = await page.evaluate(() => {
      const el = document.getElementById('appSync');
      const r = el.getBoundingClientRect();
      return r.top + window.scrollY - 300;
    });
    await page.evaluate((v) => window.scrollTo(0, Math.max(0, v)), y);
    await wait(300);

    // 用页面里直接 click() 而不是 puppeteer 的几何点击：
    // 工具栏是 sticky 的，几何点击会挑「不可点」报错
    const clicked = await page.evaluate(() => {
      const b = document.getElementById('appSync');
      if (!b) return { ok: false, why: '页面上没有 #appSync' };
      if (b.disabled) return { ok: false, why: '按钮是禁用状态' };
      b.click();
      return { ok: true, why: '' };
    });
    await wait(2600);   // 走完「同步中…」→ 出提示

    const st = await page.evaluate(() => {
      const b = document.getElementById('appSync');
      const t = document.getElementById('toast');
      return {
        按钮: b && b.textContent,
        可点: b && !b.disabled,
        提示: t && t.textContent,
        按钮在视口内: b ? (() => {
          const r = b.getBoundingClientRect();
          // 只看 top/bottom 不够：display:none 的元素 rect 全是 0，也会判成「在视口内」
          return r.width > 0 && r.top >= 0 && r.bottom <= innerHeight + 1;
        })() : null,
        同步说明行: (document.getElementById('appSyncInfo') || {}).textContent,
      };
    });
    const out = path.join(SAMPLES, SYNC_FILE);
    await page.screenshot({ path: out, fullPage: false });
    report.push({ 文件: SYNC_FILE, 视口: W + 'x760', 点击: clicked, ...st, 报错: errs });
    await page.close();
  }

  await browser.disconnect();
  console.log(JSON.stringify(report, null, 1));
})();
