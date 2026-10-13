// 给 samples/ 出四张扩展截图：托管平台成功页、扩展确认卡、真站点不再误报、
// 右侧常驻的「记一笔」标签。用法：先 `safe-browser serve`，再
//   BROWSER_URL=http://127.0.0.1:<port> node extension/test/make-shots.mjs
import { createRequire } from "module";
import path from "path";
import fs from "fs";
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
// 夹具先拷到短路径再打开：卡片上会显示当前网址，直接从仓库目录打开的话
// 截图里会带上本机的用户名和目录结构，而 samples/ 是要进仓库的。
const FXD = "/tmp/at-fx";
fs.rmSync(FXD, { recursive: true, force: true });
fs.cpSync(path.join(HERE, "fixtures"), FXD, { recursive: true });
const FX = "file://" + FXD + "/";
const OUT = path.join(HERE, "..", "..", "samples") + "/";
const CONTENT = path.join(HERE, "..", "content.js");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const STUB = (g, seed) => {
  window.__guess = g || {};
  // seed 用来预置设置项，比如把右侧「记一笔」标签关掉（免得挡住卡片）或换个高度
  const store = Object.assign({}, seed || {});
  window.chrome = {
    runtime: {
      sendMessage: (msg, cb) => {
        let res = { ok: true };
        if (msg && msg.type === "guess") res = Object.assign({ ok: true }, window.__guess);
        else if (msg && msg.type === "companies") res = Object.assign({ ok: true, hit: "", sure: false, cands: [], known: [] }, window.__dict || {});
        else if (msg && msg.type === "save") res = { ok: true, duplicate: false, total: 1 };
        if (cb) cb(res);
        return Promise.resolve(res);
      },
      onMessage: { addListener: (fn) => { window.__onMsg = fn; } },
      lastError: null,
    },
    storage: {
      local: {
        get: (k, cb) => { const o = {}; (Array.isArray(k) ? k : [k]).forEach((x) => { o[x] = store[x]; }); if (cb) cb(o); },
        set: (o, cb) => { Object.assign(store, o); if (cb) cb(); return Promise.resolve(); },
      },
      onChanged: { addListener: () => {} },
    },
  };
};

(async () => {
  const browser = await puppeteer.connect({ browserURL: BROWSER_URL, defaultViewport: { width: 1100, height: 700 } });

  // 托管平台的投递成功页：页面上没有岗位名，公司名从「感谢您应聘 XX 校招」里认出来，
  // 岗位名从上一个页面（岗位详情页）带过来
  let page = await browser.newPage();
  await page.setViewport({ width: 1100, height: 780, deviceScaleFactor: 2 });
  await page.evaluateOnNewDocument(STUB, {}, { at_fab: false });
  await page.goto(FX + "xyzsuccess.html", { waitUntil: "load" });
  // FX 是 Node 这边的变量，页面里拿不到 —— 必须当参数传进去
  await page.evaluate((fx) => {
    sessionStorage.setItem("at.lastjob.v1", JSON.stringify({
      host: "", company: "星桥科技", position: "投资管理岗（上海）",
      url: fx + "xyzjob.html", at: Date.now(),
    }));
  }, FX);
  await page.addScriptTag({ path: CONTENT });
  await sleep(1600);
  await page.screenshot({ path: OUT + "shot-ext-success.png" });
  await page.close();

  // 新版确认卡（点完投递，页面提示成功）
  page = await browser.newPage();
  await page.setViewport({ width: 1100, height: 700, deviceScaleFactor: 2 });
  await page.evaluateOnNewDocument(STUB, {}, { at_fab: false });
  await page.goto(FX + "apply-flow.html", { waitUntil: "load" });
  await page.addScriptTag({ path: CONTENT });
  await sleep(600);
  await page.click("#btn");
  await sleep(1400);
  await page.screenshot({ path: OUT + "shot-extension.png" });
  await page.close();

  // 页面上本来就写着「已投递」的岗位页 —— 那是状态标签，不是这次投递动作，不该弹卡。
  // 用本地夹具而不是真站点：截图要能离线重现，且不把真实公司的页面收进仓库。
  page = await browser.newPage();
  await page.setViewport({ width: 1100, height: 760, deviceScaleFactor: 2 });
  await page.evaluateOnNewDocument(STUB, {}, { at_fab: false });
  await page.goto(FX + "sea-like.html", { waitUntil: "load" });
  await sleep(1200);
  await page.addScriptTag({ path: CONTENT });
  await sleep(1200);
  await page.evaluate(() => {
    const d = document.createElement("div");
    d.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:2147483647;" +
      "background:#fffbe8;border:1px solid #e8d59a;border-radius:10px;padding:8px 12px;" +
      "font:13px -apple-system,'PingFang SC',sans-serif;color:#7a5a12";
    d.textContent = "页面上本来就有「已投递」——这是状态，不是动作，不弹卡";
    document.body.appendChild(d);
  });
  await sleep(600);
  await page.screenshot({ path: OUT + "shot-ext-nopop.png" });
  await page.close();

  // 右边那颗常驻的「记一笔」：完全不依赖任何识别，投完自己点一下。
  // 这是自动识别漏了之后的兜底路，截图就取"点开手动卡"的状态。
  page = await browser.newPage();
  await page.setViewport({ width: 1100, height: 700, deviceScaleFactor: 2 });
  // 标签故意拖到偏上（at_fab_top）—— 默认垂直居中，会被卡片挡住一半
  await page.evaluateOnNewDocument(STUB, {}, { at_fab_top: 10 });
  await page.goto(FX + "sea-like.html", { waitUntil: "load" });
  await page.addScriptTag({ path: CONTENT });
  await sleep(900);
  await page.evaluate(() => {
    const host = document.querySelector('[data-at="fab"]');
    host.shadowRoot.querySelector(".tab").click();
  });
  await sleep(1500);
  await page.screenshot({ path: OUT + "shot-ext-fab.png" });
  await page.close();

  await browser.disconnect();
  console.log("done");
})().catch((e) => { console.error("FAILED: " + e.stack); process.exit(1); });
