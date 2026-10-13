/* 投递记录助手 —— 后台脚本
   负责把记录 POST 给本机的 applytracker 服务。
   服务没起来时不丢数据：先存进浏览器本地队列，下次连上自动补记。 */

const PORT_KEY = "at_port";
const QUEUE_KEY = "at_queue";
const DEFAULT_PORT = 8788;

async function getPort() {
  const o = await chrome.storage.local.get(PORT_KEY);
  return Number(o[PORT_KEY]) || DEFAULT_PORT;
}
function base(port) {
  return "http://127.0.0.1:" + port;
}

async function post(path, body) {
  const port = await getPort();
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 6000);
  try {
    const r = await fetch(base(port) + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    return await r.json();
  } finally {
    clearTimeout(t);
  }
}

async function queueOf() {
  const o = await chrome.storage.local.get(QUEUE_KEY);
  return Array.isArray(o[QUEUE_KEY]) ? o[QUEUE_KEY] : [];
}
async function pushQueue(item) {
  const q = await queueOf();
  q.push({ ...item, queued_at: new Date().toISOString() });
  await chrome.storage.local.set({ [QUEUE_KEY]: q.slice(-200) });
  return q.length;
}
async function flushQueue() {
  const q = await queueOf();
  if (!q.length) return { flushed: 0, left: 0 };
  const left = [];
  let flushed = 0;
  for (const item of q) {
    try {
      const d = await post("/api/apply", item);
      if (d && d.ok) flushed++;
      else left.push(item);
    } catch (e) {
      left.push(item);
    }
  }
  await chrome.storage.local.set({ [QUEUE_KEY]: left });
  return { flushed, left: left.length };
}

async function health() {
  const port = await getPort();
  try {
    const r = await fetch(base(port) + "/api/health", { cache: "no-store" });
    const d = await r.json();
    return { ok: !!(d && d.ok), version: (d && d.version) || "" };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
}

/* 抓公司名只能靠猜，猜错的多（招聘网站上标题往往是项目名）。
   看板里已经有她真实投过的记录，拿链接回去比对，比猜准得多。 */
async function guess(url) {
  const port = await getPort();
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 4000);
  try {
    const r = await fetch(base(port) + "/api/apply-guess?url=" + encodeURIComponent(url || ""),
      { cache: "no-store", signal: ctrl.signal });
    return await r.json();
  } catch (e) {
    return { ok: false, company: "", position: "", confident: false };
  } finally {
    clearTimeout(t);
  }
}

/* 拿这一页的文字去问她自己的公司名单：这页上出现的是哪家。 */
async function companies(text) {
  const port = await getPort();
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 4000);
  try {
    const r = await fetch(base(port) + "/api/companies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text || "" }),
      signal: ctrl.signal,
    });
    return await r.json();
  } catch (e) {
    return { ok: false, hit: "", sure: false, cands: [], known: [] };
  } finally {
    clearTimeout(t);
  }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    if (msg && msg.type === "save") {
      try {
        const d = await post("/api/apply", msg.payload);
        if (d && d.ok) {
          await flushQueue();
          reply({ ok: true, duplicate: !!d.duplicate, total: d.total });
        } else {
          reply({ ok: false, error: (d && d.error) || "服务返回了异常结果" });
        }
      } catch (e) {
        const n = await pushQueue(msg.payload || {});
        reply({ ok: false, queued: true, queuedCount: n, error: "本地服务没连上" });
      }
      return;
    }
    if (msg && msg.type === "health") {
      reply(await health());
      return;
    }
    if (msg && msg.type === "guess") {
      reply(await guess(msg.url));
      return;
    }
    if (msg && msg.type === "companies") {
      reply(await companies(msg.text));
      return;
    }
    if (msg && msg.type === "flush") {
      const d = await flushQueue();
      reply({ ok: true, ...d });
      return;
    }
    if (msg && msg.type === "queueCount") {
      const q = await queueOf();
      reply({ ok: true, count: q.length });
      return;
    }
    reply({ ok: false, error: "unknown message" });
  })();
  return true; // 异步回复
});

chrome.runtime.onStartup.addListener(() => { flushQueue(); });
chrome.runtime.onInstalled.addListener(() => { flushQueue(); });
