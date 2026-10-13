/* 投递记录助手 —— 内容脚本
 *
 * 只在「你确实投了这一下」的时候弹卡。判据有且只有两条：
 *   ① 你先点了提交 / 投递 / 申请 这类按钮，之后页面上冒出一句成功提示；
 *   ② 你落在一个明确的「投递成功结果页」上（网址里带 success / submitted 之类）。
 *
 * 只是打开一个招聘页、页面上写着「已投递」，不算 —— 那是状态标签，
 * 往往表示你早就投过了。老版本把状态当动作，进官网就弹卡，就是这么来的。
 *
 * 成功提示的判断分三层：枚举词表（准）→ 语序无关的宽松判据（广，只认短句）
 * → 整页跳转后的结果页正文（只认词表，免得长文里凑出「申请 + 成功」）。
 * 点过提交之后哪怕浏览器整页换了，也会把这次「刚投过」带到下一页去。
 *
 * 但「监听 DOM 变动」有个天生的盲区：整页跳转过来的结果页上，那句提示是页面
 * **自带**的、不是「刚冒出来」的，Observer 一辈子看不到。所以页面一加载先跑一遍
 * harvestExisting()，把**已经写在页面上**的短提示块收进队列（智联的
 * 「恭喜你已成功投递该企业！」就是这么漏掉过一次真实投递的）。
 *
 * 判据再补也还是「猜」，站点换个说法就漏一次。所以另有一条**完全不猜**的路：
 * 页面右侧常驻一颗竖排的「记一笔」标签（mountFab），投完自己点一下，两秒记上，
 * 跟自动弹卡互不影响。它由 popup 里的开关控制，每个站点可以单独屏蔽。
 *
 * 自动填的公司 / 岗位分「读到的」和「猜的」两类，猜的那类一律高亮 + 在卡片上
 * 写明来源（见 showBubble 里的 warnText），别让她以为页面上真这么写着。
 * 公司名认不出来的第一道兜底是**拿她自己的名单当字典**（askCompanies →
 * POST /api/companies）：把这一页的正文发给本机服务，去投递记录 / 待投递 /
 * 机会库里比对（README「3. 正常投简历」里有完整说明）。
 * 抓取逻辑的离线回归在 extension/test/extract-test.mjs。
 */

(() => {
  "use strict";

  // ---- 只认「事情刚刚发生过」的说法 ----
  // 「已投递」故意不在表里：它是状态，不是动作。
  const SUCCESS_EVENTS = [
    "投递成功", "简历投递成功", "投递已成功", "申请成功", "提交成功", "投递完成",
    "网申成功", "报名成功", "申请已提交", "投递已提交", "简历已提交", "简历提交成功",
    "申请提交成功", "报名提交成功", "感谢投递", "感谢您的投递",
    // 意思一样、语序不同的写法。mokahr 收尾只写「已成功提交申请！」，此前整个认不出，
    // 卡片根本不弹（实测 2026-10-01 某托管站的岗位页）。这一类站点不少，一起补上。
    "已成功提交申请", "成功提交申请", "申请已成功提交", "提交申请成功",
    "已成功提交", "提交已成功", "投递已提交成功", "报名已成功", "网申已完成",
    "申请已收到", "简历已收到", "感谢您投递", "投递已收到", "简历投递完成",
    "Application submitted", "Successfully submitted", "Application received",
    "Your application has been submitted", "Thanks for applying", "Thank you for applying",
    "successfully applied", "application has been received",
  ];
  // 结果页网址里常见的那几个词（放在路径 / 主机 / hash 分段上比对）
  const SUCCESS_URL_SEG = /(success|submitted|thankyou|thanks|applicationreceived|formsubmitted|applicationcomplete|applycomplete|applydone|submitdone|submitok|applyok|applysucceed|complete)/;

  // 光靠枚举词必然漏：同一个意思有几十种写法，站点每改一次版就多一种。
  // 所以再给一条「语序无关」的判据 —— 一句话里同时有动作词和成功词就算数。
  // 前提是它得短（≤80 字）：长段落里「申请」和「成功」各出现一次不算数。
  const OK_ACTION = /(提交|投递|申请|报名|网申|递交|apply|submit)/i;
  const OK_DONE = /(成功|完成|已提交|已收到|已受理|received|submitted|complete|success)/i;
  // 这些是「状态 / 入口」，不是「刚发生」：我的投递、投递记录、投递进度…
  const OK_STOP = /(投递记录|投递进度|投递查询|投递状态|我的投递|申请记录|申请进度|简历记录|继续申请|再次申请|申请职位)/;

  function looksLikeSuccessText(raw) {
    const s = txt(raw);
    if (!s || s.length > 80) return false;
    const n = s.replace(/[\s\u3000,，。.！!？?、;；:：~～…\-—_·|｜]+/g, "");
    if (OK_STOP.test(n)) return false;
    // 「已投递」是状态标签，不是「刚刚投完了」
    if (/(已投递|待投递|未投递|已结束)/.test(n) && !/(成功|完成)/.test(n)) return false;
    return OK_ACTION.test(n) && OK_DONE.test(n);
  }

  // 一段文字算不算「刚投完」：先走词表（准），再走宽松判据（广）
  function hitIn(raw) {
    const s = txt(raw);
    if (!s) return "";
    const low = s.toLowerCase();
    for (const w of SUCCESS_EVENTS) if (low.includes(w.toLowerCase())) return w;
    return looksLikeSuccessText(s) ? "投递成功" : "";
  }
  // 这些站点永远不碰（含看板自己、邮箱、搜索引擎）
  const SKIP_HOSTS = [
    "127.0.0.1", "localhost", "feishu.cn", "larksuite.com", "mail.qq.com", "webmail",
    "google.com", "bing.com", "baidu.com", "deepseek.com", "workbuddy.cn", "docs.qq.com",
  ];
  // ……但「整域跳过」粒度太粗：飞书招聘的**企业校招官网**恰好挂在
  // `<企业代号>.jobs.feishu.cn` 上（很多公司的校招入口就是它），
  // 跟着 feishu.cn 一起跳掉的话，这类站上「记一笔」标签、自动弹卡、图标记录
  // 三条路同时失效 —— 页面右侧光秃秃一个入口都没有。
  // 2026-10-02 她投完一家就撞在这上面（结果页「简历已收到」，右侧什么都没有）。
  // 例外放在跳过判定之前，只放行招聘官网这个子域，飞书文档 / 消息 / HR 端照旧不碰。
  const ALLOW_HOSTS = ["jobs.feishu.cn", "jobs.larksuite.com"];
  const JOB_HINTS = [
    "招聘", "校招", "校园招聘", "投递", "网申", "岗位", "职位", "简历", "求职", "应聘", "申请",
    "career", "campus", "recruit", "talent", "job", "apply", "zhaopin", "hr.", "yingjiesheng",
    "mokahr", "51job", "zhiye", "liepin", "lagou", "nowcoder", "hotjob",
  ];
  const COMPANY_SUFFIX = /(有限责任公司|股份有限公司|有限公司|集团有限公司|集团|银行|证券|保险|科技|网络|控股|股份|公司)$/;
  const TITLE_SPLIT = /\s*[|\-_—–·]\s*/;
  // 这些字样只出现在页面框架 / 系统提示里，绝不是岗位名
  const BAD_TEXT = /(招聘|首页|登录|注册|投递成功|校园招聘|职位搜索|全部职位|网站地图|隐私|帮助|关于我们|确认投递信息|投递信息|提示|公告|通知|下载|用户协议|版本|反馈|客服|二维码|扫码|关注我们|返回|更多|申请职位|立即投递|我要投递|投递简历|立即申请|马上申请|查看详情|点击查看|分享|收藏|返回顶部|加载中|请稍候)/;
  const VERSION_RE = /\bv?\d+\.\d+(?:\.\d+)?\b/i;

  // ---- 明写着「公司 / 单位 / 岗位」的地方（最可信的一条路）----
  // 国聘、智联、北森、51job 这类站会把「应聘单位 / 应聘职位」成对排在信息栏里，
  // 页面上明写的字比 logo、比网页标题都准，所以放在抽取链最前面。
  // 长标签（≥3 字：应聘单位、招聘职位…）是站点专门为这两个字段起的名字；
  // 短的（企业、公司、岗位）可能只是栏目词，单独排后面且不当「确信」。
  const CO_LABELS = [
    "招聘单位", "用人单位", "应聘单位", "用工单位", "所属单位", "公司名称", "公司全称",
    "企业名称", "单位名称", "招聘企业", "所属公司", "招聘公司", "雇主单位", "雇主",
    "企业", "公司",
  ];
  const POS_LABELS = [
    "应聘职位", "应聘岗位", "招聘职位", "招聘岗位", "岗位名称", "职位名称", "应聘岗位名称",
    "所投职位", "投递职位", "职位", "岗位",
  ];
  // 标签后面跟着这些词，说明那是栏目名（「公司简介」「单位地址」），不是值
  const LABEL_SECTION =
    /(简介|介绍|概况|规模|性质|地址|电话|邮箱|官网|人数|行业|福利|描述|详情|要求|职责|信息|类别|类型|所在地|所在地区|介绍)/;
  // 值里出现这些，说明捞到的是别处的文字（按钮、导航、空状态、或是另一个标签）
  // 最后一串是标签词本身：mokahr 的「职位信息」表格里标签那行会出现两遍
  // （标签 + 没渲染出值的占位），不挡掉就会把「职位名称」当成岗位名填进去。
  const VALUE_BAD =
    /(暂无|未填写|保密|点击|请选择|请输入|以上|如下|同上|不限|更多|登录|注册|下载|查看|展开|收起|首页|全部|搜索|筛选|排序|我的|消息|退出|返回|下一页|上一页|职位名称|岗位名称|职位信息|岗位信息|职位性质|岗位性质|职位类别|岗位类别|职位描述|职位详情|岗位职责|工作地点|招聘人数|学历要求|工作性质|薪资|发布时间|更新时间|应聘信息|基本信息|求职意向|公司名称|单位名称|应聘单位|招聘单位|企业名称|所属部门)/;
  // 公司名宽松后缀：研究院、研究所、医院、学校…这些也是正经单位
  const CO_SUFFIX_LOOSE =
    /(公司|集团|研究院|研究所|银行|证券|保险|学校|大学|学院|医院|中心|工厂|车站|出版社|事务所|局|院|厂|所|社|店|部|行|司)$/;

  // 提交类按钮：点了之后打开 25 秒的「可能刚投完」窗口
  const SUBMIT_RE = /(提交|投递|申请|报名|网申|立即申请|马上申请|确认提交|保存并提交|submit|apply|send application)/i;
  // 但这些不是「投」的按钮（我的投递 / 投递记录 / 投递进度 之类都是查看入口）
  const SUBMIT_STOP_RE = /(投递记录|投递进度|我的投递|投递查询|投递状态|申请记录|申请进度|投递助手|已投递|投递成功)/;
  const ARM_WINDOW = 25000;

  const seenKey = (href) => "at_seen:" + href;

  function txt(v) {
    return String(v == null ? "" : v).replace(/\s+/g, " ").trim();
  }
  function hasCJK(s) {
    return /[\u4e00-\u9fa5]/.test(s);
  }
  function visible(el) {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const st = getComputedStyle(el);
    return st.display !== "none" && st.visibility !== "hidden" && Number(st.opacity || 1) > 0.05;
  }

  // ---------------- 页面判断 ----------------
  function skipHere() {
    const h = (location.hostname || "").toLowerCase();
    // 例外优先：招聘官网是「要干活的地方」，不能被所属平台的整域规则连坐
    if (ALLOW_HOSTS.some((x) => h === x || h.endsWith("." + x))) return false;
    if (SKIP_HOSTS.some((x) => h === x || h.endsWith("." + x))) return true;
    return false;
  }
  function looksLikeJobPage() {
    const hay = (location.href + " " + document.title + " " + (location.hostname || "") + " " +
      (location.pathname || "")).toLowerCase();
    if (JOB_HINTS.some((k) => hay.includes(k.toLowerCase()))) return true;
    const body = txt(document.body ? document.body.innerText : "").slice(0, 6000);
    let n = 0;
    for (const w of ["岗位", "职位", "简历", "投递", "应聘", "招聘"]) if (body.includes(w)) n++;
    return n >= 2;
  }
  // 网址本身就像「投完了」的结果页
  function looksLikeSuccessUrl() {
    let parts = [];
    try {
      const u = new URL(location.href);
      parts = [u.hostname].concat(u.pathname.split("/")).concat(
        u.hash.replace(/^#\/?/, "").split("/"));
    } catch (e) {
      return false;
    }
    return parts.some((p) => {
      const s = String(p || "").toLowerCase().replace(/[^a-z0-9]/g, "");
      return s.length >= 6 && s.length <= 40 && SUCCESS_URL_SEG.test(s);
    });
  }

  // ---------------- 抽取公司 / 岗位 ----------------
  // 这些字样说明抓到的东西不是公司名：招聘站框架文字、动作词、项目名
  const CO_BAD = /(招聘|校招|校园|求职|简历|网申|首页|登录|注册|系统|平台|人才|官网|计划|项目|管理培训生|管培生|实习生|投递|申请|提交|成功|我们|您|你的|公司名称|自动|提示|版权所有|扫码|关注|返回|更多)/;
  // 公司名后面常跟的尾巴，得洗掉
  const BRAND_TAIL = /(校园招聘|秋季招聘|春季招聘|社会招聘|招聘官网|招聘网站|招聘系统|校招|秋招|春招|社招|网申|招聘|暑期实习|实习生)$/;

  // 把「星桥科技校招」「星桥科技2027校园招聘」这类洗回「星桥科技」；洗不干净就返回空
  function brand(raw) {
    let s = txt(raw);
    if (!s) return "";
    s = s.replace(/^[^\u4e00-\u9fa5A-Za-z]+/, "").replace(/[\s·|｜\-—–_]+$/g, "");
    for (let i = 0; i < 2; i++) {
      s = s.replace(BRAND_TAIL, "").replace(/[\s·|｜\-—–_]+$/g, "").replace(/\d{4}\s*$/i, "");
    }
    s = txt(s);
    if (s.length < 2 || s.length > 20) return "";
    if (CO_BAD.test(s)) return "";
    if (!hasCJK(s) && !/^[A-Za-z][A-Za-z0-9&.\-]{1,19}$/.test(s)) return "";
    return s;
  }

  // ① 页面上明写着「应聘单位 / 应聘职位」这类标签的地方
  //
  //    用 innerText 按行扫，不按元素扫 —— 两个好处：
  //    ① 站点把标签和值拆成两个 div 也好、写在同一行也好，都能认；
  //    ② innerText 只含可见文字，搜索框的历史下拉、隐藏的推荐位天然被排除，
  //       不会像扫 class 那样把「万象商贸（北京）有限公司」从下拉里捞出来。
  function tidyValue(raw) {
    let s = txt(raw);
    if (!s) return "";
    s = s.replace(/^[·•●○◆■□◇▪▫※★☆※\-\s]+/, "").replace(/[：:，,、；;｜|\s]+$/, "").trim();
    if (s.length < 2 || s.length > 34) return "";
    if (VALUE_BAD.test(s) || LABEL_SECTION.test(s)) return "";
    if (/[。！？；]/.test(s)) return "";
    if (!hasCJK(s) && !/^[A-Za-z][A-Za-z0-9&.,\- ]{1,30}$/.test(s)) return "";
    return s;
  }

  function labelValue(labels) {
    const body = document.body ? document.body.innerText : "";
    if (!body) return null;
    const lines = body.split("\n").map(txt).filter(Boolean).slice(0, 400);
    for (const L of labels) {
      const sameLine = new RegExp("^" + L + "\\s*[:：]\\s*(.+)$");
      const alone = new RegExp("^" + L + "\\s*[:：]?$");
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.length > 40) continue;
        const m = line.match(sameLine);
        if (m) {
          const v = tidyValue(m[1]);
          if (v) return { value: v, label: L, strong: L.length >= 3 };
          continue;
        }
        if (alone.test(line)) {
          const v = tidyValue(lines[i + 1] || "");
          if (v) return { value: v, label: L, strong: L.length >= 3 };
        }
      }
    }
    return null;
  }

  function companyFromLabels() {
    const hit = labelValue(CO_LABELS);
    if (!hit) return { value: "", sure: false };
    // 「企业 / 公司」这种二字标签可能只是栏目词，值对不上单位后缀就只当参考
    return { value: hit.value, sure: hit.strong && CO_SUFFIX_LOOSE.test(hit.value), from: "labels" };
  }

  function positionFromLabels(company) {
    const hit = labelValue(POS_LABELS);
    if (!hit) return { value: "", sure: false };
    const v = hit.value;
    if (v === company) return { value: "", sure: false };
    if (CO_SUFFIX_LOOSE.test(v)) return { value: "", sure: false };  // 捞成公司了
    if (!okPosition(v)) return { value: "", sure: false };
    return { value: v, sure: hit.strong, from: "labels" };
  }

  // ② 页面上明确标着「这是公司名」的地方，且带着公司/集团/银行… 后缀
  function companyStrict() {
    const out = [];
    const push = (v, node) => {
      const s = txt(v);
      if (!s || s.length > 30) return;
      if (node) {
        if (!visible(node)) return;
        // 搜索框的历史下拉、导航菜单、推荐位/职位列表里出现的公司名，
        // 十有八九是**别的**公司（实测就是这么被填上「万象商贸（北京）有限公司」的）
        if (node.closest("nav,[class*=search],[class*=suggest],[class*=dropdown],[class*=menu]," +
                         "[class*=recommend],[class*=hotjob],[class*=hot-],[class*=list-item]," +
                         "[class*=history],[class*=option]")) return;
      }
      out.push(s);
    };
    for (const sel of ['meta[property="og:site_name"]', 'meta[name="application-name"]']) {
      const m = document.querySelector(sel);
      if (m && m.content) push(m.content, null);
    }
    document.querySelectorAll('img[class*=logo][alt], .logo img[alt], header img[alt], a[class*=logo] img[alt]')
      .forEach((el2) => push(el2.getAttribute("alt"), el2));
    document.querySelectorAll(
      '[class*=company], [class*=corp], [class*=employer], [class*=firm], [id*=company], [class*=org-name]'
    ).forEach((el2) => push(el2.textContent, el2));
    txt(document.title).split(TITLE_SPLIT).forEach((seg) => push(seg, null));
    for (const c of out) {
      const s = txt(c).replace(/[（(]\s*[)）]/g, "");
      if (s.length >= 2 && s.length <= 24 && COMPANY_SUFFIX.test(s)) {
        return { value: s, sure: true, from: "strict" };
      }
    }
    return { value: "", sure: false };
  }

  // ③ 投递成功页的正文 —— 最容易写全公司名的地方
  //    「感谢您应聘星桥科技校招」「您的简历已发送至 XX 招聘」这类句式
  function companyFromSuccessText() {
    const body = txt(document.body ? document.body.innerText : "");
    if (!body) return { value: "", sure: false };
    const head = body.slice(0, 2500);
    const pats = [
      /感谢(?:您|你)?\s*(?:应聘|投递|申请|报名|关注)\s*[:：]?\s*([^\s,，。.！!？?、;；:：【】\[\]（）()「」<>]{2,20})/,
      /(?:您的)?简历已(?:被)?(?:成功)?(?:发送|投递|提交|递交)(?:至|给)\s*([^\s,，。.！!？?、;；:：]{2,20})/,
      /(?:已|成功)?(?:投递|申请|应聘)(?:至|到)\s*([^\s,，。.！!？?、;；:：]{2,20})/,
      /([^\s,，。.！!？?、;；:：]{2,16})(?:校园招聘|校招|招聘)(?:系统|官网|平台|网站)/,
    ];
    for (const re of pats) {
      const m = head.match(re);
      if (!m) continue;
      const b = brand(m[1]);
      if (b) return { value: b, sure: true, from: "success" };
    }
    return { value: "", sure: false };
  }

  // ④ 页头品牌区（logo 图片的 alt、logo 文字）
  function companyFromBrand() {
    const out = [];
    document.querySelectorAll(
      'header img[alt], [class*=logo] img[alt], [id*=logo] img[alt], [class*=brand] img[alt],' +
      'img[class*=logo][alt], [class*=nav] img[alt], [class*=topbar] img[alt]'
    ).forEach((el) => out.push(el.getAttribute("alt")));
    document.querySelectorAll(
      'header [class*=logo], header [class*=brand], [class*=logo-text], [class*=brand-name],' +
      '[class*=logoText], [class*=company-name]'
    ).forEach((el) => out.push(el.textContent));
    for (const raw of out) {
      const b = brand(raw);
      if (b && b.length <= 14) return { value: b, sure: true, from: "brand" };
    }
    return { value: "", sure: false };
  }

  // ⑤ 网页标题的头部（「星桥科技2027校园招聘」→ 星桥科技），最弱，只当参考
  function companyFromTitle() {
    const seg = txt(document.title).split(TITLE_SPLIT)[0];
    const b = brand(seg);
    if (b && b.length <= 14) return { value: b, sure: false, from: "title" };
    return { value: "", sure: false };
  }

  // 五条路按可信度依次试；都没结果时用「上一个页面见过的公司」（mem）
  function companyBest(mem) {
    for (const c of [companyFromLabels(), companyStrict(), companyFromSuccessText(), companyFromBrand()]) {
      if (c.value) return c;
    }
    if (mem && mem.company) return { value: mem.company, sure: true, from: "memory" };
    return companyFromTitle();
  }

  function okPosition(s) {
    if (!s || s.length < 2 || s.length > 34) return false;
    if (BAD_TEXT.test(s) || VERSION_RE.test(s)) return false;
    if (/[。！？；]/.test(s)) return false;
    return true;
  }

  function pickPosition(company) {
    // ① 页面上写着「应聘职位 / 岗位名称」的地方，最可信
    const byLabel = positionFromLabels(company);
    if (byLabel.value) return byLabel;

    const cands = [];
    // ② SPA 里为了截断，常把完整岗位名放在 title 属性上（mokahr 就是这么干的）。
    //    排除按钮 / 导航，那些 title 是「申请职位」这类动作词，不是岗位名。
    document.querySelectorAll("[title]").forEach((el) => {
      const t = txt(el.textContent);
      const a = txt(el.getAttribute("title"));
      if (!t || t !== a || t.length > 34) return;
      if (el.closest("button,a,[role=button],input,select,label,nav,header,footer")) return;
      if (!visible(el)) return;
      cands.push({ v: t, strong: true, from: "dom" });
    });
    // ③ 明摆着的「岗位名」容器
    document.querySelectorAll(
      '[class*=jobTitle], [class*=job-title], [class*=job_name], [class*=job-name],' +
      '[class*=jobname], [class*=position-name], [class*=positionName],' +
      '[class*=post-name], [class*=postName], [class*=jobPosition]'
    ).forEach((el) => {
      const t = txt(el.textContent);
      if (t && t.length <= 40) cands.push({ v: t, strong: true, from: "dom" });
    });
    // ④ 兜底：网址里带中文的段落（有些站点把岗位名编进路径）
    decodeURIComponent(location.pathname + location.search + location.hash)
      .split(/[/?&=#]/).forEach((seg) => {
        const s = txt(seg);
        if (s.length >= 4 && s.length <= 24 && hasCJK(s)) cands.push({ v: s, strong: false, from: "url" });
      });

    for (const c of cands) {
      const s = txt(c.v);
      if (!okPosition(s)) continue;
      if (company && s === company) continue;
      return { value: s, sure: c.strong, from: c.from };
    }
    return { value: "", sure: false };
  }

  // 岗位名优先用「上一个页面见过的」（岗位详情页 → 投递成功页，成功页本身没有岗位名）
  function positionBest(mem, company) {
    if (mem && mem.position && mem.position !== company) {
      return { value: mem.position, sure: true, from: "memory" };
    }
    return pickPosition(company);
  }

  // ---------------- 跨页记忆：「你刚才看的那个岗位」 ----------------
  // 投递成功页上往往既没有公司全名也没有岗位名，但它通常是紧接着岗位详情页出现的。
  // 同一网站内换页时 sessionStorage 共享，所以把上一个页面认出的东西存在这里。
  const LASTJOB = "at.lastjob.v1";

  function looksLikeJobDetail() {
    let hasApplyBtn = false;
    const btnSel = "button,a,[role=button],input[type=submit],input[type=button]";
    for (const el of document.querySelectorAll(btnSel)) {
      const t = txt(el.innerText || el.value || el.getAttribute("aria-label") || "");
      if (t.length >= 2 && t.length <= 12 && SUBMIT_RE.test(t) && !SUBMIT_STOP_RE.test(t)) {
        hasApplyBtn = true;
        break;
      }
    }
    if (!hasApplyBtn) return false;
    const hay = (location.href + " " + document.title).toLowerCase();
    return JOB_HINTS.some((k) => hay.includes(k.toLowerCase()));
  }

  function readMemory() {
    try { return JSON.parse(sessionStorage.getItem(LASTJOB) || "null"); } catch (e) { return null; }
  }

  // 在岗位详情页留下「这家公司这个岗位」；成功页读出来用
  function rememberJob() {
    if (skipHere()) return;
    const prev = readMemory();
    if (prev && prev.url === location.href) return;             // 这一页刚记过
    if (looksLikeSuccessUrl() || noticeHit()) return;           // 成功页别把结果页信息盖上去
    const pos = pickPosition("").value;
    if (!pos) return;                                           // 连岗位名都没有，不值得记
    const co = companyFromLabels().value || companyStrict().value ||
               companyFromBrand().value || companyFromSuccessText().value || "";
    if (!looksLikeJobDetail() && !co) return;
    try {
      sessionStorage.setItem(LASTJOB, JSON.stringify({
        host: location.hostname, company: co, position: pos, url: location.href, at: Date.now(),
      }));
    } catch (e) { /* 隐私模式下 sessionStorage 不可用，忽略 */ }
  }

  function recallJob() {
    const o = readMemory();
    if (!o || typeof o !== "object") return null;
    if ((o.host || "") !== (location.hostname || "")) return null;  // 换网站了，不认
    if (o.url === location.href) return null;                   // 就是这一页，没意义
    if (Date.now() - (o.at || 0) > 3 * 3600 * 1000) return null; // 超过 3 小时不要
    return o;
  }

  // ---------------- 问本机服务：这个链接以前记过吗 ----------------
  function askGuess(url) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v || {}); } };
      const timer = setTimeout(() => finish({}), 3000);
      try {
        chrome.runtime.sendMessage({ type: "guess", url }, (res) => {
          clearTimeout(timer);
          if (chrome.runtime.lastError) return finish({});
          finish(res || {});
        });
      } catch (e) {
        clearTimeout(timer);
        finish({});
      }
    });
  }

  // ---- 问本机服务：这一页的文字里，出现的是我记过的哪家公司 ----
  // 招聘站上"公司名该从哪读"没有通用答案（标题常是项目名、logo 有时是 svg、
  // 信息栏各家叫法不同）。但她自己的名单是确定的 —— 投递记录、待投递、机会库里
  // 出现过的几千个名字，页面上只要出现其中一个，那几乎就是它。
  function askCompanies(text) {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v || {}); } };
      const timer = setTimeout(() => finish({}), 3000);
      try {
        chrome.runtime.sendMessage({ type: "companies", text }, (res) => {
          clearTimeout(timer);
          if (chrome.runtime.lastError) return finish({});
          finish(res || {});
        });
      } catch (e) {
        clearTimeout(timer);
        finish({});
      }
    });
  }

  function pageTextOf() {
    try {
      // 只取前 2500 字：公司名基本都在头部 / 正文开头，而且再长请求体就没必要了
      return txt(document.body ? document.body.innerText : "").slice(0, 2500);
    } catch (e) { return ""; }
  }

  // ---------------- 弹卡片 ----------------
  let box = null;      // shadow host
  let busyCard = false; // 正在异步组装卡片，别重复弹
  let openedUrl = "";  // 弹卡时那一页的地址（存库用这个，不用点「记下来」时的地址）
  let busy = false;

  function closeBox() {
    if (box) {
      box.remove();
      box = null;
    }
  }

  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === "style") n.style.cssText = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    });
    (children || []).forEach((c) => n.appendChild(typeof c === "string" ? document.createTextNode(c) : c));
    return n;
  }

  const CARD_CSS = `
    :host{all:initial}
    .card{position:fixed;right:18px;bottom:18px;width:326px;background:#fff;color:#15171c;
      border:1px solid #e5e7eb;border-radius:14px;padding:14px 15px 13px;
      box-shadow:0 8px 30px rgba(16,24,40,.18);font:13px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
    .hd{display:flex;align-items:center;gap:8px;margin-bottom:10px}
    .hd b{font-size:13.5px;font-weight:650}
    .badge{font-size:11px;color:#2f7d5d;border:1px solid #9ed3bd;border-radius:999px;padding:0 7px;line-height:1.7;flex:none}
    .x{margin-left:auto;border:none;background:transparent;color:#9aa0a8;font-size:16px;cursor:pointer;padding:0 2px}
    label{display:block;font-size:11.5px;color:#6f747d;margin:8px 0 3px}
    input,select{width:100%;box-sizing:border-box;padding:6px 9px;border:1px solid #e5e7eb;border-radius:8px;
      background:#f7f8fa;color:#15171c;font:13px inherit;font-family:inherit}
    input.uncertain{border-color:#dcae5a;background:#fffaf0}
    input:focus,select:focus{outline:none;border-color:#3b5bdb;background:#fff}
    .url{font-size:11.5px;color:#9aa0a8;margin-top:8px;word-break:break-all}
    .warn{font-size:11.5px;color:#a26414;margin-top:7px;line-height:1.55}
    .ft{display:flex;gap:8px;align-items:center;margin-top:12px}
    button{padding:6px 13px;border:1px solid #e5e7eb;border-radius:999px;background:transparent;color:#15171c;
      font-size:12.5px;font-family:inherit;cursor:pointer}
    .ok{border-color:#3b5bdb;background:#3b5bdb;color:#fff}
    .msg{font-size:11.5px;margin-left:auto;color:#6f747d;max-width:150px;text-align:right}
    .msg.ok{color:#2f7d5d}
    .msg.err{color:#c9403a}
    .mute{display:block;margin-top:9px;font-size:11px;color:#9aa0a8;background:none;border:none;
      padding:0;cursor:pointer;text-decoration:underline}
    @media (prefers-color-scheme:dark){
      .card{background:#1d1f23;color:#e8e9ed;border-color:#2b2e34;box-shadow:0 8px 30px rgba(0,0,0,.45)}
      .hd b{color:#e8e9ed}
      label{color:#9aa0a8}
      input,select{background:#24272c;border-color:#2b2e34;color:#e8e9ed}
      input.uncertain{border-color:#8a6526;background:#2c261b}
      input:focus,select:focus{background:#1d1f23;border-color:#7c93f5}
      button{color:#e8e9ed;border-color:#2b2e34}
      .ok{border-color:#7c93f5;background:#7c93f5;color:#131417}
      .x{color:#6e737b}
      .badge{color:#7bc4a2;border-color:#3d6a56}
      .warn{color:#d5a05a}
      .mute{color:#6e737b}
    }`;

  async function showBubble(hit, opts) {
    closeBox();
    const manual = !!(opts && opts.manual);

    openedUrl = location.href;                       // 先把地址冻住
    const guess = await askGuess(openedUrl);         // 问本机服务：以前记过吗
    const dict = await askCompanies(pageTextOf());   // 这一页的文字里，是我记过的哪家
    const mem = recallJob();                         // 上一个页面（岗位详情页）见过的
    // 优先级：她自己的名单里认出来的 > 链接命中老记录 > 读这一页 > 同一个站的历史
    let co = dict.hit
      ? { value: dict.hit, sure: true, from: "dict" }
      : (guess.confident && guess.company)
        ? { value: guess.company, sure: true, from: "history" }
        : companyBest(mem);
    if (!co.value && guess.company) {                // 整站只投过一家的参考值
      co = { value: guess.company, sure: false, from: "history-host" };
    }
    const pos = guess.position
      ? { value: guess.position, sure: true, from: "history" }
      : positionBest(mem, co.value);

    const host = el("div", { "data-at": "card",
      style: "all:initial;position:fixed;z-index:2147483647;" });
    const root = host.attachShadow({ mode: "open" });
    root.appendChild(el("style", {}, [CARD_CSS]));

    const inCompany = el("input", { type: "text", value: co.value, placeholder: "公司名称（没认出来，手填一下）" });
    const inPosition = el("input", { type: "text", value: pos.value, placeholder: "岗位名称（没认出来，手填一下）" });
    const inStatus = el("select", {});
    ["已投递", "简历通过", "测评笔试", "面试", "Offer", "已结束"].forEach((s) =>
      inStatus.appendChild(el("option", {}, [s])));
    // 备注留空自己填：不做任何自动抓取，也不预填 —— 自动猜出来的东西写进备注里
    // 比不写更糟（她得逐条核对）。跟看板页「手动加一条」的 fNote 是同一个字段。
    const inNote = el("input", { type: "text", placeholder: "比如内推码、面试反馈" });

    // 公司名给一个自动补全：候选＝这一页上认出来的 + 她最近打交道的那些公司。
    // 认不到也不至于从零打字。datalist 必须和 input 在同一棵 shadow 树里才生效。
    const coList = el("datalist", { id: "at-co" });
    const coSeen = {};
    (dict.cands || []).concat(dict.known || []).forEach((c) => {
      const k = String(c || "").trim();
      if (!k || k.length > 40 || coSeen[k]) return;
      coSeen[k] = 1;
      coList.appendChild(el("option", { value: k }, []));
    });
    if (coList.childElementCount) inCompany.setAttribute("list", "at-co");
    const msg = el("span", { class: "msg" });

    async function save() {
      if (busy) return;
      const payload = {
        company: inCompany.value.trim(),
        position: inPosition.value.trim(),
        url: openedUrl || location.href,
        status: inStatus.value,
        note: inNote.value.trim(),
        source: manual ? "扩展(手动)" : "扩展",
      };
      if (!payload.company) {
        msg.textContent = "公司名得填一个";
        msg.className = "msg err";
        inCompany.focus();
        return;
      }
      busy = true;
      msg.textContent = "记着…";
      msg.className = "msg";
      try {
        const res = await chrome.runtime.sendMessage({ type: "save", payload });
        if (res && res.ok) {
          msg.textContent = res.duplicate ? "这条之前记过了" : "记好了 ✓";
          msg.className = "msg ok";
          setTimeout(closeBox, 1300);
        } else if (res && res.queued) {
          msg.textContent = "服务没启动，先存着，起来后自动补记";
          msg.className = "msg err";
        } else {
          msg.textContent = "没记上：" + ((res && res.error) || "未知原因");
          msg.className = "msg err";
        }
      } catch (e) {
        msg.textContent = "扩展和后台断了，刷新页面再试";
        msg.className = "msg err";
      }
      busy = false;
    }

    function muteHost() {
      const h = location.hostname || "";
      chrome.storage.local.get("at_block", (o) => {
        const list = Array.isArray(o.at_block) ? o.at_block : [];
        if (h && list.indexOf(h) < 0) list.push(h);
        chrome.storage.local.set({ at_block: list });
      });
      closeBox();
    }

    // 名字要是从别处带过来的，就直说来源，别让她以为是这一页读出来的
    const memFrom = (co.from === "memory" && pos.from === "memory") ? "公司和岗位名"
      : (co.from === "memory") ? "公司名"
        : (pos.from === "memory") ? "岗位名" : "";
    const labFrom = (co.from === "labels" && pos.from === "labels") ? "公司和岗位名"
      : (co.from === "labels") ? "公司名"
        : (pos.from === "labels") ? "岗位名" : "";
    let warnText;
    if (co.from === "dict") {
      warnText = "公司名是在这一页的文字里认出来的（你自己记过这家），核对一下岗位名就行。";
    } else if (memFrom) {
      warnText = memFrom + "取自你刚打开的那个岗位页，点「记下来」之前核对一下。";
    } else if (co.from === "history-host") {
      warnText = "公司名来自历史记录 —— 这台网站你以前只记过「" + co.value +
        "」这一家，是它吗？不是就改一下再记。";
    } else if (labFrom) {
      warnText = labFrom + "是从页面上「应聘单位 / 应聘职位」这类标签里读的，核对一下。";
    } else if (manual) {
      warnText = "公司和岗位名尽量从这一页读了，读不到就手填一下。";
    } else {
      warnText = "自动认的可能不准，点「记下来」之前核对一下公司名和岗位名。";
    }

    const card = el("div", { class: "card" }, [
      el("div", { class: "hd" }, [
        el("b", {}, [manual ? "记一次投递" : "看起来刚投完"]),
        el("span", { class: "badge" }, [manual ? "手动" : (hit || "投递成功")]),
        el("button", { class: "x", title: "先不记", onclick: closeBox }, ["×"]),
      ]),
      el("label", {}, ["公司"]),
      inCompany,
      el("label", {}, ["岗位"]),
      inPosition,
      el("label", {}, ["当前状态"]),
      inStatus,
      el("label", {}, ["备注（选填）"]),
      inNote,
      el("div", { class: "url" }, [(openedUrl || location.href).slice(0, 120)]),
      coList,
      // 手动卡也给来源提示：预填了名字却不说是哪来的，她只能靠猜
      el("div", { class: "warn" }, [warnText]),
      el("div", { class: "ft" }, [
        el("button", { class: "ok", onclick: save }, ["记下来"]),
        el("button", { onclick: closeBox }, ["忽略"]),
        msg,
      ]),
      manual ? null : (location.hostname ? el("button", { class: "mute", onclick: muteHost },
        ["这个网站以后别再自动弹（" + location.hostname + "）"]) : null),
    ].filter(Boolean));
    root.appendChild(card);
    document.documentElement.appendChild(host);
    box = host;
    if (!co.sure) { inCompany.className = "uncertain"; inCompany.placeholder = "自动没认准，手动填一下"; }
    if (!pos.sure) { inPosition.className = "uncertain"; inPosition.placeholder = "自动没抓到，手动填一下"; }
    setTimeout(() => inCompany.focus(), 60);
  }

  // ---------------- 侦测 ----------------
  // 只收「刚冒出来的、窄小的提示块」，不再满篇扫页面上的字。
  let fresh = [];
  let lastTick = 0;
  let pending = null;
  let armedAt = 0;          // 最近一次点提交类按钮的时间
  let autoOn = true;        // 设置里的自动识别开关
  let blockHosts = [];      // 用户说过「别再弹」的站点

  // ---------------- 常驻「记一笔」侧边标签 ----------------
  // 自动识别永远是"猜"：站点换个说法就漏一次 —— moka 写「已成功提交申请」、
  // 智联写「恭喜你已成功投递该企业」，都是这套判据认不出的写法，漏一次她就得
  // 手补一次。所以再给一条**完全不依赖任何识别**的路：一个常驻的小标签，
  // 投完自己点一下，两秒记上，跟自动弹卡互不影响。
  // 贴右边、垂直居中，跟另一个扩展右下角那颗蓝色胶囊错开，不会打架。
  const FAB_ON_KEY = "at_fab";
  const FAB_BLOCK_KEY = "at_fab_block";
  const FAB_TOP_KEY = "at_fab_top";
  let fab = null;
  let fabOn = true;
  let fabBlock = [];
  let fabTop = 45;          // 垂直位置（vh），可以拖着挪
  let fabMoved = false;

  const FAB_CSS = `
    :host{all:initial}
    .tab{position:fixed;right:0;writing-mode:vertical-rl;text-orientation:upright;
      background:#3b5bdb;color:#fff;letter-spacing:2px;padding:10px 7px;
      border-radius:9px 0 0 9px;cursor:pointer;user-select:none;-webkit-user-select:none;
      font:12px/1 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
      box-shadow:-1px 1px 9px rgba(16,24,40,.24);opacity:.85;
      transition:opacity .15s ease,padding .15s ease;z-index:2147483646}
    .tab:hover{opacity:1;padding-right:10px}
    .tab .x{position:absolute;top:-7px;left:-7px;width:15px;height:15px;border-radius:50%;
      background:#98a0b3;color:#fff;writing-mode:horizontal-tb;
      font:10px/15px -apple-system,BlinkMacSystemFont,sans-serif;text-align:center;
      cursor:pointer;display:none}
    .tab:hover .x{display:block}
    @media (prefers-color-scheme:dark){
      .tab{background:#7c93f5;color:#141519;box-shadow:-1px 1px 9px rgba(0,0,0,.4)}
      .tab .x{background:#5c6273}
    }`;

  // 什么时候该出现：没关掉、不是免打扰站点、没被这个站屏蔽，
  // 而且「看着像招聘页」或者「她刚点过提交」—— 后者让冷门站点也认得住。
  function fabAllowed() {
    if (!fabOn) return false;
    if (skipHere()) return false;
    // 空 hostname 不拦：只有回归测试里的 file:// 页面会这样，真站点永远是 http(s)。
    // 曾经在这里写成 `if (!h || ...) return false`，结果 file:// 下标签永远不出现。
    const h = (location.hostname || "").toLowerCase();
    if (h && fabBlock.indexOf(h) >= 0) return false;
    return looksLikeJobPage() || !!armedAt;
  }

  function mountFab() {
    if (fab) { fab.remove(); fab = null; }
    if (!fabAllowed()) return;
    if (!document.body) { setTimeout(mountFab, 400); return; }

    const host = el("div", { "data-at": "fab", style: "all:initial;" });
    const root = host.attachShadow({ mode: "open" });
    root.appendChild(el("style", {}, [FAB_CSS]));

    const tab = el("div", { class: "tab", title: "记一次投递（不用等自动识别）" }, ["记一笔"]);
    tab.style.top = fabTop + "vh";
    const x = el("div", { class: "x", title: "这个网站不再显示" }, ["×"]);
    tab.appendChild(x);

    let dragging = false;
    let startY = 0;
    let startTop = 0;
    const onMove = (e) => {
      if (!dragging) return;
      const dy = e.clientY - startY;
      if (Math.abs(dy) > 4) fabMoved = true;
      if (!fabMoved) return;
      const vh = window.innerHeight || 800;
      fabTop = Math.max(8, Math.min(88, startTop + (dy / vh) * 100));
      tab.style.top = fabTop + "vh";
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      document.removeEventListener("mousemove", onMove, true);
      document.removeEventListener("mouseup", onUp, true);
      if (fabMoved) { try { chrome.storage.local.set({ [FAB_TOP_KEY]: fabTop }); } catch (e) { /* 存不上就下次回到默认位置 */ } }
    };
    tab.addEventListener("mousedown", (e) => {
      if (e.target === x) return;
      dragging = true;
      fabMoved = false;
      startY = e.clientY;
      startTop = fabTop;
      document.addEventListener("mousemove", onMove, true);
      document.addEventListener("mouseup", onUp, true);
      e.preventDefault();
    });
    tab.addEventListener("click", (e) => {
      if (e.target === x) return;
      if (fabMoved) return;                       // 刚才是在拖，不算点
      showBubble("", { manual: true });
    });
    x.addEventListener("click", (e) => {
      e.stopPropagation();
      const h = (location.hostname || "").toLowerCase();
      try {
        chrome.storage.local.get(FAB_BLOCK_KEY, (o) => {
          const list = Array.isArray(o[FAB_BLOCK_KEY]) ? o[FAB_BLOCK_KEY] : [];
          if (h && list.indexOf(h) < 0) list.push(h);
          chrome.storage.local.set({ [FAB_BLOCK_KEY]: list });
        });
      } catch (e2) { /* 存不上也先藏起来 */ }
      if (fab) { fab.remove(); fab = null; }
    });

    root.appendChild(tab);
    document.documentElement.appendChild(host);
    fab = host;
  }

  // 先按默认值挂上，读回设置后再校正一次 —— 读不到设置也不至于没有入口
  mountFab();
  try {
    chrome.storage.local.get([FAB_ON_KEY, FAB_BLOCK_KEY, FAB_TOP_KEY], (o) => {
      if (!o) return;
      fabOn = o[FAB_ON_KEY] !== false;
      fabBlock = Array.isArray(o[FAB_BLOCK_KEY]) ? o[FAB_BLOCK_KEY] : [];
      if (typeof o[FAB_TOP_KEY] === "number") fabTop = o[FAB_TOP_KEY];
      mountFab();
    });
    chrome.storage.onChanged.addListener((ch) => {
      let again = false;
      if (ch[FAB_ON_KEY]) { fabOn = ch[FAB_ON_KEY].newValue !== false; again = true; }
      if (ch[FAB_BLOCK_KEY]) {
        fabBlock = Array.isArray(ch[FAB_BLOCK_KEY].newValue) ? ch[FAB_BLOCK_KEY].newValue : [];
        again = true;
      }
      if (again) mountFab();
    });
  } catch (e) { /* 存储读不到，保持默认 */ }

  // 整页跳转的站点：点提交 → 浏览器整个换页 → 内存里的 armedAt 没了，
  // 于是结果页上的「投递成功」谁也不认。同一个网站内 sessionStorage 是共享的，
  // 所以把这次「刚投过」带过去。（SPA 站点不受影响，本来就在同一页。）
  const ARMED_KEY = "at.arm.v1";
  const POPPED_KEY = "at.popped.v1";
  const scriptAt = Date.now();
  let armedFromNav = false;   // 这一页的「刚点过提交」是从上一页带过来的
  let bodyScanned = false;    // 结果页正文只扫一次

  function arm() {
    armedAt = Date.now();
    armedFromNav = false;
    bodyScanned = false;
    mountFab();          // 她动手投递了，冷门站点也把「记一笔」摆出来
    try { sessionStorage.setItem(ARMED_KEY, String(armedAt)); } catch (e) { /* 隐私模式 */ }
  }
  (function resumeArm() {
    try {
      const v = Number(sessionStorage.getItem(ARMED_KEY) || 0);
      if (v && Date.now() - v <= ARM_WINDOW) {
        armedAt = v;
        armedFromNav = true;
      }
    } catch (e) { /* 读不到就算了 */ }
  })();

  chrome.storage.local.get(["at_auto", "at_block"], (o) => {
    if (o && o.at_auto === false) autoOn = false;
    if (o && Array.isArray(o.at_block)) blockHosts = o.at_block;
  });
  chrome.storage.onChanged.addListener((ch) => {
    if (ch.at_auto) autoOn = ch.at_auto.newValue !== false;
    if (ch.at_block) blockHosts = Array.isArray(ch.at_block.newValue) ? ch.at_block.newValue : [];
  });

  const CAND_SEL = '[role=alert],[role=dialog],[class*=toast],[class*=message],[class*=modal],' +
                   '[class*=dialog],[class*=result],[class*=success],[class*=tip],[class*=notice],[class*=alert]';

  // 像不像一个「提示条 / 弹层」：字少、元素少。可见性留到真正判断时再看，
  // 有的提示条刚插进来还在淡入动画里，这会儿量不到尺寸。
  function looksLikeNotice(node) {
    if (node.nodeType !== 1) return false;
    const t = txt(node.innerText);
    if (t.length < 2 || t.length > 200) return false;
    // 字少就放宽节点数上限：提示条里常带一个图标（svg 里还有 path），
    // 卡死 12 会把 mokahr 那种「✓ + 一句话」的提示块挡在外面。
    if (node.querySelectorAll("*").length > (t.length <= 80 ? 40 : 12)) return false;
    const tag = (node.tagName || "").toLowerCase();
    if (tag === "html" || tag === "body" || tag === "script" || tag === "style") return false;
    return true;
  }

  function harvest(node) {
    if (node.nodeType !== 1) return;
    if (looksLikeNotice(node)) fresh.push({ node, text: txt(node.innerText), at: Date.now() });
    if (!node.querySelectorAll) return;
    node.querySelectorAll(CAND_SEL).forEach((el2) => {
      if (looksLikeNotice(el2)) fresh.push({ node: el2, text: txt(el2.innerText), at: Date.now() });
    });
  }

  // 把「页面上已经存在」的提示块也收一遍。
  // 为什么需要：整页跳转过来的结果页，那句「投递成功」是页面自带的，
  // 不是"刚冒出来"的，MutationObserver 永远看不到 —— 以前就只剩正文兜底那条路，
  // 而它故意只认词表（怕长文里凑出"申请 + 成功"），于是
  // 「恭喜你已成功投递该企业！」这种写法整条漏掉（2026-10-02 一条真实投递）。
  // 先用 textContent 过一遍（不触发布局），只有短文本才值得再读 innerText。
  function harvestExisting() {
    if (!document.body) return;
    let all;
    try { all = document.body.querySelectorAll("*"); } catch (e) { return; }
    for (let i = 0; i < all.length && i < 6000; i++) {
      if (fresh.length >= 400) break;
      const n = all[i];
      const tc = n.textContent;
      if (!tc || tc.length < 2 || tc.length > 200) continue;
      if (n.querySelectorAll("*").length > 40) continue;
      if (!looksLikeNotice(n)) continue;
      fresh.push({ node: n, text: txt(n.innerText), at: Date.now() });
    }
  }

  // 只看「刚冒出来的提示条」，不碰页面正文 —— 谁都能调用，不会消耗状态
  function noticeHit() {
    if (!armedAt) return "";                         // 这一页你根本没点过提交，就不认
    const now = Date.now();
    for (let i = fresh.length - 1; i >= 0; i--) {
      const f = fresh[i];
      if (now - f.at > ARM_WINDOW) continue;
      if (f.at < armedAt) continue;                  // 必须发生在点提交之后
      if (!f.node.isConnected) continue;
      const h = hitIn(f.text);
      if (h) return h;
    }
    return "";
  }

  function findHit() {
    const h = noticeHit();
    if (h) return h;
    // 整页跳转的站点：提示直接写进结果页正文，之后不会再有 DOM 变动可抓。
    // 只在「点提交是在上一页、且离开不足 ARM_WINDOW」时扫一次，而且只认词表里的说法
    // —— 整页文字里「申请」「成功」太容易凑齐，宽松判据在这里会误报。
    if (armedFromNav && !bodyScanned && Date.now() - scriptAt > 1200) {
      bodyScanned = true;
      const low = txt(document.body ? document.body.innerText : "").slice(0, 3000).toLowerCase();
      for (const w of SUCCESS_EVENTS) if (low.includes(w.toLowerCase())) return w;
    }
    return "";
  }

  function tick() {
    if (skipHere() || box || busyCard || !autoOn) return;
    if (blockHosts.indexOf(location.hostname || "") >= 0) return;

    const eventHit = findHit();                                    // ① 点过提交 + 冒出成功提示
    const urlHit = eventHit ? "" : (looksLikeSuccessUrl() && looksLikeJobPage() ? "投递成功页" : "");
    const hit = eventHit || urlHit;
    if (!hit) return;
    if (!looksLikeJobPage()) return;
    // 同一页只自动弹一次，除非你手动点
    try {
      if (sessionStorage.getItem(seenKey(location.href))) return;
      sessionStorage.setItem(seenKey(location.href), "1");
      // 同一次投递别弹两次：点了提交那一页弹过之后，整页跳到结果页再认出来也不再弹。
      // 按「这一次提交」记，不按时间记 —— 否则同一网站上连投两家会被误挡。
      // 网址自带 success 的结果页不走这条（那种没有「这一次提交」可言），仍靠 seenKey 去重。
      if (armedAt) {
        if (Number(sessionStorage.getItem(POPPED_KEY) || 0) === armedAt) return;
        sessionStorage.setItem(POPPED_KEY, String(armedAt));
      }
    } catch (e) { /* 隐私模式下 sessionStorage 可能不可用，忽略 */ }
    busyCard = true;
    showBubble(hit, { manual: false }).catch(() => {}).then(() => { busyCard = false; });
  }

  let lastRemember = 0;
  function rememberSoon() {                 // 别每批 DOM 变动都跑一遍
    const now = Date.now();
    if (now - lastRemember < 2000) return;
    lastRemember = now;
    try { rememberJob(); } catch (e) { /* 记不住就算了，不影响弹卡 */ }
  }

  function schedule() {
    rememberSoon();
    const now = Date.now();
    if (now - lastTick < 500) {
      if (pending) clearTimeout(pending);
      pending = setTimeout(() => { pending = null; tick(); }, 500);
      return;
    }
    lastTick = now;
    tick();
  }

  if (!skipHere()) {
    // 点了「提交 / 投递 / 申请」这类按钮，才打开后面的识别窗口
    document.addEventListener("click", (e) => {
      if (!e.isTrusted) return;
      let n = e.target;
      for (let i = 0; i < 4 && n && n.nodeType === 1; i++, n = n.parentElement) {
        const t = txt(n.innerText || n.value || n.getAttribute("aria-label") || "");
        if (!t || t.length > 20) continue;
        if (SUBMIT_STOP_RE.test(t)) break;
        if (SUBMIT_RE.test(t)) { arm(); break; }
      }
    }, true);

    const mo = new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType === 1) harvest(n);
        }
      }
      // 上限放宽些：SPA 一次重渲染能加几百个节点，别把提示条挤出去
      while (fresh.length > 400) fresh.shift();
      schedule();
    });
    try {
      mo.observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) { /* 极少数页面 documentElement 还没就绪 */ }

    let lastUrl = location.href;
    setInterval(() => {
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        closeBox();
        bodyScanned = false;      // 换路由了，允许把新页面的正文再扫一次
        schedule();
      }
    }, 1500);

    // 静态的成功结果页进来时不会再有 DOM 变动，得自己先看一次
    harvestExisting();
    schedule();

    // 整页跳转过来的结果页同样不会有 DOM 变动，但正文扫描要等 1.2 秒后才有意义
    // （SPA 常常这时才把内容渲染出来），所以补一次迟到的检查。
    if (armedFromNav) setTimeout(schedule, 1400);
  }

  // 扩展图标 →「记录当前页」时由 popup 发过来
  chrome.runtime.onMessage.addListener((msg, sender, reply) => {
    if (msg && msg.type === "manual") {
      showBubble("", { manual: true });
      reply({ ok: true });
      return true;
    }
    if (msg && msg.type === "ping") {
      reply({ ok: true, skip: skipHere(), url: location.href });
      return true;
    }
    return false;
  });
})();
