/* 扩展抓取的离线回归 —— 在真浏览器里喂合成页面，看弹卡时自动填了什么。
 *
 * 为什么要这套：抓取逻辑依赖真实 DOM（innerText 分行、可见性、MutationObserver），
 * 纯文本断言测不出来。四个夹具覆盖：
 *   ① 国聘式「应聘单位 / 应聘职位」信息栏 + 搜索下拉里挂着别家公司（陷阱）
 *   ② 老站点（logo alt + .job-title 类名）—— 不许因为改版而退化
 *   ③ 「公司名称：XX」同一行的写法
 *   ④ 页面上什么都没有 —— 只能靠历史记录兜底，但必须标成「来源：历史记录」并高亮
 *
 * 用法：先 `safe-browser serve`，再
 *   BROWSER_URL=http://127.0.0.1:<port> node extension/test/extract-test.mjs
 */
import { createRequire } from "module";
import path from "path";
import { fileURLToPath } from "url";

const require = createRequire(import.meta.url);
const puppeteer = require(process.env.HOME +
  "/.workbuddy/binaries/node/workspace/node_modules/puppeteer-core");

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTENT = path.join(HERE, "..", "content.js");
const FIXDIR = path.join(HERE, "fixtures");

let ok = true;
function check(name, cond, extra) {
  console.log((cond ? "  ✓ " : "  ✗ ") + name + (cond || !extra ? "" : "  ← " + extra));
  ok = ok && !!cond;
}

const CASES = [
  {
    name: "国聘式信息栏（下拉里挂着万象商贸，不许抓它）",
    file: "iguopin-apply.html",
    click: "#apply",
    guess: {},
    company: "华宇新能源集团重庆研究院有限公司",
    position: "市场营销岗",
    uncertain: false,
    warnHas: "标签",
  },
  {
    name: "老站点（logo alt + job-title 类名）不许退化",
    file: "moka-detail.html",
    click: "#apply",
    guess: {},
    company: "星桥科技",
    position: "管理培训生",
    uncertain: false,
  },
  {
    name: "「公司名称：XX」同一行的写法",
    file: "label-inline.html",
    click: "#apply",
    guess: {},
    company: "长风物流有限公司",
    position: "通用管理岗",
    uncertain: false,
  },
  {
    name: "标签那行重复出现（mokahr 职位信息表）→ 不许把「职位名称」当岗位名",
    file: "label-repeat.html",
    click: "#apply",
    guess: {},
    company: "",
    position: "Yunxi全球管理培训生",
    uncertain: true,   // 公司名这一页真没有 → 留空 + 黄框（宁可她手填）
  },
  {
    name: "页面上什么都没有 → 历史兜底，必须高亮 + 说清来源",
    file: "bare-nothing.html",
    click: "#apply",
    guess: {
      company: "万象商贸（北京）有限公司", position: "", matched: "host",
      confident: false, evidence: "这台网站你以前只记过这一家公司",
    },
    company: "万象商贸（北京）有限公司",
    position: "",
    uncertain: true,
    warnHas: "历史记录",
  },
];

const browser = await puppeteer.connect({
  browserURL: process.env.BROWSER_URL || "http://127.0.0.1:9222",
  defaultViewport: { width: 1100, height: 900 },
});

for (const c of CASES) {
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e.message)));
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text()); });

  // 扩展 API 的最小替身 —— 必须在 content.js 之前注入
  await page.evaluateOnNewDocument((g) => {
    window.__GUESS = g;
    const noop = () => {};
    window.chrome = {
      storage: {
        local: { get: (k, cb) => { const o = {}; if (cb) cb(o); return Promise.resolve(o); },
                 set: () => Promise.resolve() },
        onChanged: { addListener: noop },
      },
      runtime: {
        sendMessage: (msg, cb) => {
          const r = (msg && msg.type === "guess") ? (window.__GUESS || {}) : { ok: true };
          if (cb) cb(r);
          return Promise.resolve(r);
        },
        onMessage: { addListener: noop },
      },
    };
  }, c.guess);

  await page.goto("file://" + path.join(FIXDIR, c.file), { waitUntil: "load" });
  await page.addScriptTag({ path: CONTENT });
  await new Promise((r) => setTimeout(r, 300));
  await page.click(c.click);

  let got = null;
  try {
    await page.waitForFunction(
      // 认卡片必须靠 host 标记：页面上还挂着「记一笔」那个 shadow host
      () => [...document.documentElement.children].some(
        (d) => d.getAttribute && d.getAttribute("data-at") === "card"), { timeout: 8000 });
    got = await page.evaluate(() => {
      const host = [...document.documentElement.children].find(
        (d) => d.getAttribute && d.getAttribute("data-at") === "card");
      const root = host.shadowRoot;
      const ins = root.querySelectorAll("input");
      const warn = root.querySelector(".warn");
      return {
        company: ins[0].value, position: ins[1].value,
        companyCls: ins[0].className, positionCls: ins[1].className,
        warn: warn ? warn.textContent : "(无提示行)",
        badge: (root.querySelector(".badge") || {}).textContent || "",
      };
    });
  } catch (e) {
    got = { company: "(卡片没弹出来)", position: "", warn: "", companyCls: "", positionCls: "" };
  }

  console.log("\n▸ " + c.name);
  check("公司 = " + c.company, got.company === c.company, "实际：" + got.company);
  if (c.position) check("岗位 = " + c.position, got.position === c.position, "实际：" + got.position);
  check(c.uncertain ? "填的值被标成「不确定」（黄框）" : "填的值是可信来源（不上黄框）",
    (got.companyCls === "uncertain") === c.uncertain, "class=" + got.companyCls);
  if (c.warnHas) check("提示里说明来源含「" + c.warnHas + "」", got.warn.includes(c.warnHas), got.warn);
  check("控制台零报错", errs.length === 0, errs.slice(0, 2).join(" | "));

  if (c.file === "iguopin-apply.html") {
    // 卡片底部会把当前页面地址写出来。用 file:// 跑测试时那是本机绝对路径
    // （带用户名、带工作区目录），而这张图要进开源仓库 —— 截图前换成公开地址。
    await page.evaluate(() => {
      const scrub = (root) => {
        root.querySelectorAll("*").forEach((el) => {
          if (el.shadowRoot) scrub(el.shadowRoot);
          const t = (el.textContent || "").trim();
          if (!el.children.length && /^file:\/\//.test(t)) {
            el.textContent = "https://www.iguopin.com/job/detail?id=216463669175779850";
          }
        });
      };
      scrub(document);
    });
    await page.screenshot({ path: "/tmp/ext-iguopin-card.png" });
  }
  if (c.file === "bare-nothing.html") {
    await page.screenshot({ path: "/tmp/ext-fallback-card.png" });
  }
  await page.close();
}

await browser.disconnect();
console.log("\n" + (ok ? "全部通过" : "有失败项"));
process.exit(ok ? 0 : 1);
