// 扩展识别逻辑回归（弹卡时机 + 名字抽取）—— 在隔离浏览器里把 content.js 当页面脚本跑。
// 重点：①「只是打开招聘页、页面上写着已投递」不许弹卡；②真投完了照样弹。
//      ⑩ 提示语序不许漏（mokahr 写「已成功提交申请！」）；⑪ 整页跳转也要接着认；
//      ⑫ 带「投递/申请」字样的状态词和长段落不许误伤；
//      ⑬ 整页跳转过来的结果页 —— 提示是页面**自带**的（MutationObserver 看不到），
//         这种「恭喜你已成功投递该企业！」也要能认；⑭ 常驻「记一笔」标签；
//      ⑮ 拿她自己的公司名单预填；⑯ 整域跳过不许连坐平台旗下的招聘官网
//         （<企业>.jobs.feishu.cn 曾被 feishu.cn 一起挡掉，右侧一个入口都没有）。
// 用法：先 `safe-browser serve`，再
//   BROWSER_URL=http://127.0.0.1:<port> node extension/test/popup-test.mjs
import { createRequire } from "module";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

// puppeteer-core 不随仓库分发（它只负责连现成的浏览器，不会去下载 Chromium）。
// 依次找：PUPPETEER_DIR → WorkBuddy 自带的全局 node_modules → 裸 require；都找不到就说人话退出。
const require = createRequire(import.meta.url);
const puppeteer = (() => {
  const dirs = [process.env.PUPPETEER_DIR,
                process.env.HOME && path.join(process.env.HOME, ".workbuddy/binaries/node/workspace/node_modules")]
    .filter(Boolean);
  for (const d of dirs) { try { return require(path.join(d, "puppeteer-core")); } catch (e) { /* 继续找 */ } }
  try { return require("puppeteer-core"); } catch (e) {
    console.error("找不到 puppeteer-core。先 npm i puppeteer-core，或用 PUPPETEER_DIR 指过去。");
    process.exit(1);
  }
})();

const HERE = path.dirname(fileURLToPath(import.meta.url));

const BROWSER_URL = process.env.BROWSER_URL || ("http://127.0.0.1:" + (process.argv[2] || "9222"));
const CONTENT = path.join(HERE, "..", "content.js");
const FX = "file://" + path.join(HERE, "fixtures") + "/";
const SHOTS = process.env.EXT_SHOTS || "/tmp/ext-shots";
fs.mkdirSync(SHOTS, { recursive: true });

const results = [];
function check(name, cond, extra = "") {
  results.push({ name, pass: !!cond, extra: cond ? "" : String(extra) });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 卡片和「记一笔」标签都是挂在 documentElement 上的 shadow host，
// 所以认卡片必须靠标记，不能靠"第一个有 shadowRoot 的"。
const CARD_HOST = `[...document.documentElement.children].find((e) => e.getAttribute && e.getAttribute("data-at") === "card")`;
const FAB_HOST = `[...document.documentElement.children].find((e) => e.getAttribute && e.getAttribute("data-at") === "fab")`;
const HAS_FAB = `(() => { const h = ${FAB_HOST}; return !!h; })()`;
const READ_FAB = `(() => {
  const h = ${FAB_HOST};
  if (!h) return null;
  const tab = h.shadowRoot.querySelector(".tab");
  return tab ? { text: tab.textContent, hasClose: !!h.shadowRoot.querySelector(".x") } : null;
})()`;
// 点侧边标签（puppeteer 的 elementHandle 进不去 shadow DOM，只能这样点）
const CLICK_FAB = `(() => {
  const h = ${FAB_HOST};
  if (!h) return false;
  h.shadowRoot.querySelector(".tab").click();
  return true;
})()`;

const READ_CARD = `(() => {
  const host = ${CARD_HOST};
  if (!host) return null;
  const r = host.shadowRoot;
  const ins = [...r.querySelectorAll("input")];
  return {
    title: (r.querySelector(".hd b") || {}).textContent || "",
    badge: (r.querySelector(".badge") || {}).textContent || "",
    company: ins[0] ? ins[0].value : "",
    position: ins[1] ? ins[1].value : "",
    // 备注框是第 3 个 input（公司 / 岗位 / 备注）。它必须是**空的** ——
    // 这个字段全靠她自己填，一旦被预填，她会以为那是抓来的、直接点「记下来」。
    note: ins[2] ? ins[2].value : "",
    notePlaceholder: ins[2] ? ins[2].placeholder : "",
    hasNoteLabel: [...r.querySelectorAll("label")].some((l) => /备注/.test(l.textContent)),
    warn: !!r.querySelector(".warn"),
    warnText: ((r.querySelector(".warn") || {}).textContent || ""),
    coOptions: [...r.querySelectorAll("datalist option")].map((o) => o.value),
  };
})()`;

async function open(browser, file, guess, seed, session) {
  return openUrl(browser, FX + file, guess, seed, session);
}

// content.js 依赖 chrome.storage / chrome.runtime，页面里得先把这两个桩上
async function stubChrome(page, guess) {
  await page.evaluateOnNewDocument((g) => {
    window.__sent = [];
    window.__guess = g || {};
    const store = {};
    window.chrome = {
      runtime: {
        sendMessage: (msg, cb) => {
          if (msg && msg.type === "save") window.__sent.push(msg);
          let res = { ok: true };
          if (msg && msg.type === "guess") res = Object.assign({ ok: true }, window.__guess);
          else if (msg && msg.type === "companies") res = Object.assign({ ok: true }, window.__dict || {});
          else if (msg && msg.type === "save") res = { ok: true, duplicate: false, total: 1 };
          if (cb) cb(res);
          return Promise.resolve(res);
        },
        onMessage: { addListener: (fn) => { window.__onMsg = fn; } },
        lastError: null,
      },
      storage: {
        local: {
          get: (keys, cb) => { const out = {}; (Array.isArray(keys) ? keys : [keys]).forEach((k) => { out[k] = store[k]; }); if (cb) cb(out); },
          set: (o, cb) => { Object.assign(store, o); if (cb) cb(); return Promise.resolve(); },
        },
        onChanged: { addListener: () => {} },
      },
    };
  }, guess);
}

async function openUrl(browser, url, guess, seed, session) {
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await stubChrome(page, guess);
  await page.goto(url, { waitUntil: "load" });
  if (seed) {
    await page.evaluate((s) => {
      sessionStorage.setItem("at.lastjob.v1", JSON.stringify(s));
    }, seed);
  }
  // 往 sessionStorage 里预置扩展自己的状态（模拟「上一页已经发生过什么」）
  if (session) {
    await page.evaluate((s) => {
      Object.keys(s).forEach((k) => sessionStorage.setItem(k, String(s[k])));
    }, session);
  }
  await page.addScriptTag({ path: CONTENT });
  return { page, errs };
}

// 在「真实主机名」上跑夹具：夹具是 file:// 时 location.hostname 是空的，
// 而「哪些站点跳过」恰恰只看主机名 —— 只有把假域名真喂给浏览器才测得到。
// 用请求拦截冒充域名，不碰网络。
async function openHosted(browser, url, fixture, opts = {}) {
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  const body = fs.readFileSync(path.join(HERE, "fixtures", fixture), "utf8");
  const origin = new URL(url).origin;
  await page.setRequestInterception(true);
  page.on("request", (req) => {
    if (req.url().indexOf(origin) === 0) {
      req.respond({ status: 200, contentType: "text/html; charset=utf-8", body });
    } else {
      // 用 204 而不是 abort()：abort 会在控制台留一条 ERR_FAILED，污染报错断言
      req.respond({ status: 204, body: "" });
    }
  });
  await stubChrome(page, opts.guess);
  await page.goto(url, { waitUntil: "load" });
  if (opts.session) {
    await page.evaluate((s) => {
      Object.keys(s).forEach((k) => sessionStorage.setItem(k, String(s[k])));
    }, opts.session);
  }
  await page.addScriptTag({ path: CONTENT });
  return { page, errs };
}

(async () => {
  const browser = await puppeteer.connect({ browserURL: BROWSER_URL, defaultViewport: { width: 1200, height: 860 } });
  const allErrs = [];
  // 留一个不动的小页：这轮用的是 safe-browser 起的 Edge，它没有自带空白标签页，
  // 最后一个用例页一 close，整个浏览器就退出，下一个 newPage 直接 Target closed
  // （2026-10-02 出图脚本踩过，这里也会踩）。这个 keeper 全程不开脚本、不关。
  const keeper = await browser.newPage();
  await keeper.goto("about:blank");

  // ① 打开岗位页，页面自带「已投递」状态标签 —— 这是原始故障场景，不能弹卡
  {
    const { page, errs } = await open(browser, "sea-like.html");
    await sleep(1500);
    const card = await page.evaluate(READ_CARD);
    check("① 进官网只看到「已投递」状态 → 不弹卡", card === null, JSON.stringify(card));
    check("① 页面里确实有「已投递」字样（否则这条测试没意义）",
      await page.evaluate(() => document.body.innerText.includes("已投递")));
    check("① 没有任何记录被提交", (await page.evaluate(() => window.__sent)).length === 0);
    await page.screenshot({ path: SHOTS + "/out-1-no-pop.png" });
    allErrs.push(...errs);
    await page.close();
  }

  // ② 点「立即投递」后冒出「投递成功」提示 —— 该弹
  {
    const { page, errs } = await open(browser, "apply-flow.html");
    await sleep(900);
    check("② 什么都没点 → 不弹", (await page.evaluate(READ_CARD)) === null);
    await page.click("#btn");
    await sleep(1200);
    const card = await page.evaluate(READ_CARD);
    check("② 点完投递、页面提示投递成功 → 弹卡", !!card, JSON.stringify(card));
    check("② 卡片标出命中词是「投递成功」", card && card.badge === "投递成功", card && card.badge);
    check("② 公司名抓到「某某科技有限公司」（带公司后缀才认）",
      card && card.company === "某某科技有限公司", card && card.company);
    check("② 卡片带核对提醒", card && card.warn === true);
    await page.screenshot({ path: SHOTS + "/out-2-pop.png" });
    if (card) {
      await page.evaluate(() => {
        const host = [...document.documentElement.children].find(
          (e) => e.getAttribute && e.getAttribute("data-at") === "card");
        host.shadowRoot.querySelector("button.ok").click();
      });
      await sleep(1800);
      const sent = await page.evaluate(() => window.__sent);
      check("② 点「记下来」发出的公司/岗位/链接对得上",
        sent.length === 1 && sent[0].payload.company === "某某科技有限公司"
        && sent[0].payload.url.indexOf("apply-flow.html") > 0, JSON.stringify(sent));
      check("② 保存后卡片收起", (await page.evaluate(READ_CARD)) === null);
    }
    allErrs.push(...errs);
    await page.close();
  }

  // ③ 点的是「我的投递记录」（查看入口），出来的是历史状态 —— 不该弹
  {
    const { page, errs } = await open(browser, "view-only.html");
    await sleep(700);
    await page.click("#btn");
    await sleep(1200);
    const card = await page.evaluate(READ_CARD);
    check("③ 点「我的投递记录」后出现「已投递」→ 不弹卡", card === null, JSON.stringify(card));
    allErrs.push(...errs);
    await page.close();
  }

  // ④ 落在明确的投递成功结果页（网址带 success）—— 不用点也该弹
  {
    const { page, errs } = await open(browser, "xyzsuccess.html");
    await sleep(1500);
    const card = await page.evaluate(READ_CARD);
    check("④ 投递成功结果页 → 不用点也弹卡", !!card, JSON.stringify(card));
    check("④ 标出是「投递成功页」", card && card.badge === "投递成功页", card && card.badge);
    check("④ 公司名从成功页正文里认出来（「感谢您应聘星桥科技校招」→ 星桥科技）",
      !!card && card.company === "星桥科技", card && card.company);
    check("④ 岗位名这一页确实没有 → 不瞎编", !!card && card.position === "", card && card.position);
    await page.screenshot({ path: SHOTS + "/out-4-url.png" });
    allErrs.push(...errs);
    await page.close();
  }

  // ⑤ 老记录里认识这个链接时，公司名直接用老记录（比猜准）
  {
    const { page, errs } = await open(browser, "apply-flow.html",
      { company: "某某科技（集团）有限公司", position: "", matched: "prefix", confident: true });
    await sleep(700);
    await page.click("#btn");
    await sleep(1300);
    const card = await page.evaluate(READ_CARD);
    check("⑤ 本机记录里有同名链接 → 公司名用记录里的，不用猜的",
      card && card.company === "某某科技（集团）有限公司", card && card.company);
    allErrs.push(...errs);
    await page.close();
  }

  // ⑦ 岗位详情页 → 把公司/岗位留在会话里，成功后跳到成功页就不用再猜
  {
    const { page, errs } = await open(browser, "xyzjob.html");
    await sleep(1800);
    const raw = await page.evaluate(() => sessionStorage.getItem("at.lastjob.v1"));
    let o = null;
    try { o = JSON.parse(raw); } catch (e) { /* 没写就是 null */ }
    check("⑦ 岗位详情页 → 把「公司+岗位」留在会话里（给下一页用）",
      !!o && o.position === "投资管理岗（上海）" && o.company === "星桥科技", raw);
    allErrs.push(...errs);
    await page.close();
  }

  // ⑧ 成功页本身没有岗位名 → 用上一个页面（同站）带过来的
  {
    const { page, errs } = await open(browser, "xyzsuccess.html", {}, {
      host: "", company: "星桥科技", position: "投资管理岗（上海）",
      url: FX + "xyzjob.html", at: Date.now(),
    });
    await sleep(1600);
    const card = await page.evaluate(READ_CARD);
    check("⑧ 成功页没有岗位名 → 用上一个页面带过来的",
      !!card && card.position === "投资管理岗（上海）", JSON.stringify(card));
    check("⑧ 公司名仍以本页正文为准（不被记忆覆盖）",
      !!card && card.company === "星桥科技", card && card.company);
    check("⑧ 用了跨页记忆时说明白（岗位名取自刚才那个岗位页）",
      !!card && /岗位名取自你刚打开的那个岗位页/.test(card.warnText || ""), card && card.warnText);
    allErrs.push(...errs);
    await page.close();
  }

  // ⑨ 别的网站留下的记忆不算数 / 太久的记忆不算数
  {
    const { page, errs } = await open(browser, "xyzsuccess.html", {}, {
      host: "other.example.com", company: "别家公司", position: "别的岗位",
      url: "https://other.example.com/xyzjob", at: Date.now(),
    });
    await sleep(1500);
    const card = await page.evaluate(READ_CARD);
    check("⑨ 别的网站留下的记忆不算数",
      !!card && card.company !== "别家公司" && card.position !== "别的岗位",
      JSON.stringify(card));
    allErrs.push(...errs);
    await page.close();
  }
  {
    const { page, errs } = await open(browser, "xyzsuccess.html", {}, {
      host: "", company: "星桥科技", position: "很久以前的岗位",
      url: FX + "xyzjob.html", at: Date.now() - 5 * 3600 * 1000,
    });
    await sleep(1500);
    const card = await page.evaluate(READ_CARD);
    check("⑨ 超过 3 小时的记忆不算数",
      !!card && card.position !== "很久以前的岗位", JSON.stringify(card));
    allErrs.push(...errs);
    await page.close();
  }

  // ⑥ 仿真实招聘页（本地夹具，离线可复现）
  //    这一组原先是在线打开某公司真实的 mokahr 招聘页跑的 —— 既会把真实企业 URL 带进仓库，
  //    又依赖外网（站点一改版测试就红）。夹具 sea-like.html 就是照那一页搭的，断言一条没少。
  {
    const { page, errs } = await open(browser, "sea-like.html");
    await sleep(1500);
    check("⑥ 仿真实页：打开岗位页 → 不弹卡", (await page.evaluate(READ_CARD)) === null);
    // 页面自己渲染的「已投递」标签（见夹具脚本），再补一枚固定在左上角的，
    // 模拟老版扩展最容易误判的那一幕（复用老版触发条件）
    await page.evaluate(() => {
      const d = document.createElement("div");
      d.className = "status-tip";
      d.style.cssText = "position:fixed;left:10px;top:10px;z-index:99999";
      d.textContent = "已投递";
      document.body.appendChild(d);
    });
    await sleep(1500);
    check("⑥ 仿真实页：页面上挂着「已投递」状态标签 → 仍然不弹卡",
      (await page.evaluate(READ_CARD)) === null, JSON.stringify(await page.evaluate(READ_CARD)));
    await page.screenshot({ path: SHOTS + "/out-6-real.png" });
    // 手动记录一张卡，看看公司/岗位抽取（老版这里填的是页面标题「2027 Yunxi全球管理培训生计划」）
    await page.evaluate(() => window.__onMsg({ type: "manual" }, {}, () => {}));
    await sleep(1200);
    const card = await page.evaluate(READ_CARD);
    check("⑥ 仿真实页：公司名不再抓成「2027 Yunxi全球管理培训生计划」",
      !!card && card.company !== "2027 Yunxi全球管理培训生计划", JSON.stringify(card));
    check("⑥ 仿真实页：岗位名抓到职位标题而不是系统文案",
      !!card && /管理培训生/.test(card.position), JSON.stringify(card));
    console.log("     （仿真实页手动卡：公司=" + (card && card.company) +
                " 岗位=" + (card && card.position) + "）");
    allErrs.push(...errs);
    await page.close();
  }

  // ⑩ mokahr 式收尾提示 —— 页面写「已成功提交申请！」，此前整个认不出，卡片根本不弹
  //    （2026-10-01 某托管站那次就是这么丢的：她以为识别不到，其实是卡片没弹）
  {
    const { page, errs } = await open(browser, "moka-apply.html");
    await sleep(900);
    check("⑩ mokahr 式岗位页：什么都没点 → 不弹", (await page.evaluate(READ_CARD)) === null);
    await page.click("#apply");
    await sleep(1400);
    const card = await page.evaluate(READ_CARD);
    check("⑩ 点「申请职位」后提示「已成功提交申请！」→ 弹卡（老版不弹）",
      !!card, JSON.stringify(card));
    check("⑩ 岗位名从 title 属性认出来（站点类名是构建哈希，只能走这条路）",
      !!card && card.position === "市场管培生-云溪营养品-广州", card && card.position);
    check("⑩ 公司名认成「云溪」（站名「云溪招聘」洗掉「招聘」）",
      !!card && card.company === "云溪", card && card.company);
    await page.screenshot({ path: SHOTS + "/out-10-moka.png" });
    allErrs.push(...errs);
    await page.close();
  }

  // ⑪ 整页跳转到结果页，而网址里没有 success 之类的字样 —— 靠 sessionStorage 把
  //    「刚点过提交」带过来。没有这个动作就一个字都不该弹（下面是同一页的对照组）。
  {
    const { page, errs } = await open(browser, "apply-result-static.html");
    await sleep(2200);
    check("⑪ 对照组：没点过提交就落在结果页 → 不弹",
      (await page.evaluate(READ_CARD)) === null, JSON.stringify(await page.evaluate(READ_CARD)));
    allErrs.push(...errs);
    await page.close();
  }
  {
    const t = Date.now();
    const { page, errs } = await open(browser, "apply-result-static.html", {}, null,
      { "at.arm.v1": t });
    await sleep(2200);
    const card = await page.evaluate(READ_CARD);
    check("⑪ 上一页点过提交 → 整页跳到结果页（网址无 success）照样弹卡",
      !!card, JSON.stringify(card));
    check("⑪ 标出命中词是「投递成功」", !!card && card.badge === "投递成功", card && card.badge);
    allErrs.push(...errs);
    await page.close();
  }
  {
    const t = Date.now();
    const { page, errs } = await open(browser, "apply-result-static.html", {}, null,
      { "at.arm.v1": t, "at.popped.v1": t });
    await sleep(2200);
    check("⑪ 同一次提交在上一页已经弹过 → 结果页不再弹第二张",
      (await page.evaluate(READ_CARD)) === null, JSON.stringify(await page.evaluate(READ_CARD)));
    allErrs.push(...errs);
    await page.close();
  }
  {
    const t = Date.now();
    const { page, errs } = await open(browser, "apply-result-static.html", {}, null,
      { "at.arm.v1": t + 1000, "at.popped.v1": t });   // 标记指的是上一次提交
    await sleep(2200);
    check("⑪ 是另一次提交（标记对不上）→ 照弹，同网站连投两家不被误挡",
      !!(await page.evaluate(READ_CARD)), JSON.stringify(await page.evaluate(READ_CARD)));
    allErrs.push(...errs);
    await page.close();
  }

  // ⑫ 点过提交 + 页面出现带「投递/申请」字样的东西 ≠ 刚投完。新加的宽松判据不许误伤。
  {
    const { page, errs } = await open(browser, "status-only.html");
    await sleep(900);
    await page.click("#btn");
    await sleep(1800);
    const card = await page.evaluate(READ_CARD);
    check("⑫ 只冒出「我的投递记录」+ 长段落 → 不弹卡（宽松判据不许误伤）",
      card === null, JSON.stringify(card));
    allErrs.push(...errs);
    await page.close();
  }

  // ⑬ 智联（xiaoyuan.zhaopin.com）那类整页跳转的结果页：成功提示是**页面自带**的，
  //    MutationObserver 看不到，而网址里没有 success 之类字样。
  //    2026-10-02 一条真实投递就是这么丢的 —— 卡片根本没弹。
  {
    const { page, errs } = await open(browser, "zhaopin-result.html");
    await sleep(2200);
    check("⑬ 对照组：没点过提交就落在结果页 → 不弹",
      (await page.evaluate(READ_CARD)) === null, JSON.stringify(await page.evaluate(READ_CARD)));
    allErrs.push(...errs);
    await page.close();
  }
  {
    const t = Date.now();
    const { page, errs } = await open(browser, "zhaopin-result.html", {}, null,
      { "at.arm.v1": t });
    await sleep(2200);
    const card = await page.evaluate(READ_CARD);
    check("⑬ 上一页点过提交 → 整页落到结果页（提示是页面自带的、语序不在词表里）照样弹卡",
      !!card, JSON.stringify(card));
    check("⑬ 标出命中词是「投递成功」", !!card && card.badge === "投递成功", card && card.badge);
    check("⑬ 公司名从页面文字里认出来（长河发展银行）",
      !!card && card.company === "长河发展银行", card && card.company);
    check("⑬ 导航里的「我的投递记录」不许被当成投递成功（宽松判据的排除项）",
      !!card && card.badge !== "我的投递记录", card && card.badge);
    await page.screenshot({ path: SHOTS + "/out-13-zhaopin.png" });
    allErrs.push(...errs);
    await page.close();
  }

  // ⑭ 常驻「记一笔」侧边标签 —— 一条完全不依赖识别的主路径
  {
    const { page, errs } = await open(browser, "apply-flow.html");
    await sleep(1500);
    check("⑭ 招聘页上常驻的「记一笔」标签在（不用等任何识别）",
      await page.evaluate(HAS_FAB), "标签没出现");
    const fab = await page.evaluate(READ_FAB);
    check("⑭ 标签上写的是「记一笔」，并且带一颗「不再显示」的叉",
      !!fab && fab.text.indexOf("记一笔") === 0 && fab.hasClose, JSON.stringify(fab));
    check("⑭ 什么都没点 → 先不弹卡（标签在，卡不该自己蹦出来）",
      (await page.evaluate(READ_CARD)) === null);
    await page.evaluate(CLICK_FAB);
    await sleep(1300);
    const card = await page.evaluate(READ_CARD);
    check("⑭ 点标签 → 直接弹出手动卡", !!card, JSON.stringify(card));
    check("⑭ 手动卡的标题是「记一次投递」、标着「手动」",
      !!card && card.title === "记一次投递" && card.badge === "手动",
      card && (card.title + "/" + card.badge));
    check("⑭ 手动卡也说明公司/岗位名是哪来的（预填了却不说来路，只能靠猜）",
      !!card && card.warn === true && (card.warnText || "").length > 0, JSON.stringify(card));
    const hasMute = await page.evaluate(() => {
      const h = [...document.documentElement.children].find(
        (e) => e.getAttribute && e.getAttribute("data-at") === "card");
      return !!(h && h.shadowRoot.querySelector("button.mute"));
    });
    check("⑭ 手动卡不显示「这个网站以后别再自动弹」那个按钮（那是自动卡才有的）",
      hasMute === false);
    await page.screenshot({ path: SHOTS + "/out-14-fab.png" });
    allErrs.push(...errs);
    await page.close();
  }

  // ⑮ 拿她自己的公司名单预填 + 公司名自动补全
  {
    const { page, errs } = await open(browser, "apply-flow.html");
    await sleep(1500);
    await page.evaluate(() => {
      window.__dict = {
        ok: true, hit: "云溪智能", sure: true,
        cands: ["云溪智能", "星桥科技"], known: ["长风物流", "云溪智能", "星桥科技"],
      };
    });
    await page.evaluate(CLICK_FAB);
    await sleep(1300);
    const card = await page.evaluate(READ_CARD);
    check("⑮ 名单里认出来的公司名直接预填（压过页面自己读的）",
      !!card && card.company === "云溪智能", JSON.stringify(card));
    check("⑮ 说清楚公司名是从这一页的文字里认出来的",
      !!card && /这一页的文字里认出来/.test(card.warnText || ""), card && card.warnText);
    check("⑮ 公司名输入框挂了自动补全候选（认不准也不用从零打字）",
      !!card && card.coOptions.length >= 3, card && JSON.stringify(card.coOptions));
    allErrs.push(...errs);
    await page.close();
  }

  // ⑯ 整域跳过不许连坐它旗下的「招聘官网」
  // 飞书招聘的企业校招官网挂在 <企业代号>.jobs.feishu.cn 上（很多公司的校招入口就是它），
  // 而 feishu.cn 在 SKIP_HOSTS 里是整域跳过 —— 结果这类站上「记一笔」标签、自动弹卡、
  // 图标记录三条路全都不出现，页面右侧光秃秃一个入口都没有。
  // 2026-10-02 她的真实投递（结果页「简历已收到」）就撞在这上面。
  {
    const url = "https://js35svcbne.jobs.feishu.cn/youcash/resume/applied";
    const { page, errs } = await openHosted(browser, url, "apply-flow.html");
    await sleep(1500);
    check("⑯ 飞书招聘官网（<企业>.jobs.feishu.cn）上「记一笔」标签必须在",
      await page.evaluate(HAS_FAB), "标签没出现 —— 是不是又被整域跳过了");
    await page.evaluate(CLICK_FAB);
    await sleep(1300);
    const card = await page.evaluate(READ_CARD);
    check("⑯ 点它就能弹「记一次投递」手动卡（不依赖任何识别的这条路真的通）",
      !!card && card.badge === "手动", JSON.stringify(card));
    await page.screenshot({ path: SHOTS + "/out-16-feishu-jobs.png" });
    allErrs.push(...errs);
    await page.close();
  }
  {
    // 对照组：页面内容一模一样，只换主机名。飞书文档必须仍然「什么都不做」，
    // 否则这条例外就开得太大了。
    const url = "https://docs.feishu.cn/docx/abcdefgh";
    const { page, errs } = await openHosted(browser, url, "apply-flow.html");
    await sleep(1500);
    check("⑯ 对照组：docs.feishu.cn 上不出现「记一笔」标签（例外只放行招聘官网）",
      (await page.evaluate(HAS_FAB)) === false, "标签不该出现");
    await page.click("#btn");          // 页面上真有个「立即投递」按钮，点了也不许开窗口
    await sleep(1400);
    check("⑯ 对照组：飞书文档上点「立即投递」也不弹卡（跳过的是整个站，不只是标签）",
      (await page.evaluate(READ_CARD)) === null);
    check("⑯ 对照组：飞书文档上没有任何记录被提交",
      (await page.evaluate(() => window.__sent)).length === 0);
    allErrs.push(...errs);
    await page.close();
  }

  // ⑰ 备注 —— 公司/岗位是猜的，备注只能她自己填。
  //    这一格不许预填、也不许自动抓：自动猜出来的备注比不写更糟（她得逐条核对，
  //    而备注本来就是为了记「只有她知道」的事，猜错了反而误导）。
  {
    const { page, errs } = await open(browser, "apply-flow.html");
    await sleep(700);
    await page.click("#btn");
    await sleep(1300);
    let card = await page.evaluate(READ_CARD);
    check("⑰ 卡片上有「备注」这一格", !!card && card.hasNoteLabel === true, JSON.stringify(card));
    check("⑰ 备注框初始是空的（全靠自己填，不预填、不自动抓）",
      !!card && card.note === "", card && JSON.stringify(card.note));
    check("⑰ 备注框写了提示语提示能填什么", !!card && !!card.notePlaceholder,
      card && card.notePlaceholder);
    await page.screenshot({ path: SHOTS + "/out-17-note-empty.png" });

    // 手填一条，走完整条链路看有没有带上
    const NOTE = "内推码 QX-2026，HR 说 3 天内回";
    await page.evaluate((v) => {
      const host = [...document.documentElement.children].find(
        (e) => e.getAttribute && e.getAttribute("data-at") === "card");
      [...host.shadowRoot.querySelectorAll("input")][2].value = v;
    }, NOTE);
    card = await page.evaluate(READ_CARD);
    check("⑰ 手填的备注留在框里（没被重渲染冲掉）", !!card && card.note === NOTE, card && card.note);
    await page.screenshot({ path: SHOTS + "/out-17-note-filled.png" });

    await page.evaluate(() => {
      const host = [...document.documentElement.children].find(
        (e) => e.getAttribute && e.getAttribute("data-at") === "card");
      host.shadowRoot.querySelector("button.ok").click();
    });
    await sleep(1600);
    const sent = await page.evaluate(() => window.__sent);
    check("⑰ 点「记下来」把备注原样带出去（卡片 → 后台 → /api/apply）",
      sent.length === 1 && sent[0].payload.note === NOTE, JSON.stringify(sent));
    allErrs.push(...errs);
    await page.close();
  }
  {
    // 对照：不填备注时出去的是空串。服务端 add_app 拿到就 .strip() 存进「备注」列，
    // 所以这里传 undefined 会变成字符串 "undefined" 落进 CSV —— 必须是 ""。
    const { page, errs } = await open(browser, "apply-flow.html");
    await sleep(700);
    await page.click("#btn");
    await sleep(1300);
    await page.evaluate(() => {
      const host = [...document.documentElement.children].find(
        (e) => e.getAttribute && e.getAttribute("data-at") === "card");
      host.shadowRoot.querySelector("button.ok").click();
    });
    await sleep(1600);
    const sent = await page.evaluate(() => window.__sent);
    check("⑰ 对照：没填备注时传空串（不是 undefined，否则 CSV 里会存成 \"undefined\"）",
      sent.length === 1 && sent[0].payload.note === "", JSON.stringify(sent));
    allErrs.push(...errs);
    await page.close();
  }

  await keeper.close();
  await browser.disconnect();

  let bad = 0;
  for (const r of results) {
    if (!r.pass) bad++;
    console.log(`  ${r.pass ? "✓" : "✗"} ${r.name}${r.pass ? "" : "  " + r.extra}`);
  }
  const realErrs = allErrs.filter((e) => !/favicon|net::ERR_FILE_NOT_FOUND/i.test(e));
  console.log(`\n  控制台报错：${realErrs.length ? realErrs.join(" | ") : "无"}`);
  console.log(bad ? `\n✗ ${bad} 项未通过` : "\n✓ 扩展回归全部通过");
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error("FAILED: " + e.stack); process.exit(1); });

