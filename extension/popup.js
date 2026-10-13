/* 投递记录助手 —— 扩展弹窗 */

const BOARD = "http://127.0.0.1:8788/";
const AUTO_KEY = "at_auto";
const BLOCK_KEY = "at_block";
const FAB_KEY = "at_fab";
const FAB_BLOCK_KEY = "at_fab_block";
const $ = (id) => document.getElementById(id);

function setSvc(state, text) {
  $("dot").className = "dot " + (state || "");
  $("svc").textContent = text;
}

async function refresh() {
  let h = { ok: false };
  try {
    h = await chrome.runtime.sendMessage({ type: "health" });
  } catch (e) {
    setSvc("err", "后台脚本没响应");
    return;
  }
  if (h && h.ok) setSvc("ok", "本地服务已连接" + (h.version ? "（v" + h.version + "）" : ""));
  else setSvc("err", "本地服务没启动");

  try {
    const q = await chrome.runtime.sendMessage({ type: "queueCount" });
    const n = (q && q.count) || 0;
    $("flush").hidden = n === 0;
    $("q").textContent = n;
    if (n) {
      $("hint").innerHTML = '<span class="pend">有 ' + n +
        " 条没记上（当时服务没开）。把服务起来后点上面的补记按钮就行。</span>";
    }
  } catch (e) { /* ignore */ }
}

async function refreshOptions() {
  const o = await chrome.storage.local.get([AUTO_KEY, BLOCK_KEY, FAB_KEY, FAB_BLOCK_KEY]);
  $("auto").checked = o[AUTO_KEY] !== false;
  $("fab").checked = o[FAB_KEY] !== false;
  // 两个屏蔽名单一起算：① 「这个网站以后别再自动弹」② 右侧标签上那颗 ×
  const n = (Array.isArray(o[BLOCK_KEY]) ? o[BLOCK_KEY] : []).length
    + (Array.isArray(o[FAB_BLOCK_KEY]) ? o[FAB_BLOCK_KEY] : []).length;
  $("muteline").hidden = n === 0;
  $("mn").textContent = n;
}

$("auto").onchange = async () => {
  await chrome.storage.local.set({ [AUTO_KEY]: $("auto").checked });
};

$("fab").onchange = async () => {
  await chrome.storage.local.set({ [FAB_KEY]: $("fab").checked });
};

$("unmute").onclick = async () => {
  await chrome.storage.local.set({ [BLOCK_KEY]: [], [FAB_BLOCK_KEY]: [] });
  refreshOptions();
};

$("manual").onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: "manual" });
    window.close();
  } catch (e) {
    $("hint").textContent = "这个页面注入不进去（浏览器内置页、扩展商店这类页面不给注入）。换到招聘页再点。";
  }
};

$("open").onclick = () => {
  chrome.tabs.create({ url: BOARD });
  window.close();
};

$("flush").onclick = async () => {
  const r = await chrome.runtime.sendMessage({ type: "flush" });
  if (r && r.left) setSvc("err", "还有 " + r.left + " 条没记上");
  else setSvc("ok", "补齐了（" + ((r && r.flushed) || 0) + " 条）");
  refresh();
};

// 把版本号写在标题后面：改完扩展要她去 edge://extensions 点刷新才生效，
// 有了这个一眼就能确认「刷新到位了没有」—— 顶栏那个版本号是浏览器给的，
// 藏在扩展卡片里，她看不到自己打开的弹窗到底是哪一版。
try { $("ver").textContent = "v" + chrome.runtime.getManifest().version; } catch (e) { /* 拿不到就不显示 */ }

refresh();
refreshOptions();
