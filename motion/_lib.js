// 概念动画模板的公用小工具(实车图 + 标注)。motion.py 渲染前把它和 gsap.min.js 一起拷进工作目录。
//
// 数据约定(motion.py 注入 window.__DATA__):
//   { subject, title, seconds, columns, rows: [{label, value, values, tag}], source }
// rows 的 value 都是核对过事实的原文 —— 模板只负责"画",数字滚动结束时必须回到原文一字不差。
(function () {
  const D = window.__DATA__ || {};
  const HF = (window.HF = {});
  HF.data = D;
  /** 风格颜色:读 _base.css / 风格文件里的变量(三元组),给 SVG 属性和 GSAP 补间用(它们认不出 var())。
   *  HF.c("acc") → HF.c("acc");HF.c("acc", 0.5) → HF.c("acc", 0.5) */
  let CS = null;
  HF.c = function (tok, a) {
    if (!CS && typeof getComputedStyle === "function") CS = getComputedStyle(document.documentElement);
    const v = (CS && CS.getPropertyValue("--" + tok).trim()) || "255, 255, 255";
    return a == null ? `rgb(${v})` : `rgba(${v}, ${a})`;
  };
  /** 画幅:根节点的 data-width / data-height(骨架按项目画幅填)。横屏 1920×1080,竖屏 1080×1920 */
  const R0 = typeof document.getElementById === "function" ? document.getElementById("root") : null;
  HF.W = (R0 && Number(R0.dataset.width)) || 1920;
  HF.H = (R0 && Number(R0.dataset.height)) || 1080;
  HF.portrait = HF.H > HF.W;
  /** 竖屏三段式分区(占画高的比例,右侧按钮区占画宽的比例)。可由 __DATA__.grid 覆盖。
   *  safeTop 顶部安全区 / title 标题区 / frame 16:9 画面带 / data 数据区 / sub 字幕区 /
   *  bottom 起是平台按钮区;right 是右侧按钮区宽度 */
  HF.grid = Object.assign({
    safeTop: 0.06, title: [0.06, 0.30], frame: [0.30, 0.616], data: [0.616, 0.80],
    sub: [0.80, 0.90], bottom: 0.90, right: 0.12,
  }, D.grid || {});
  /** 竖屏三段式里画面带(16:9)与数据区的像素位置 */
  HF.band = { top: Math.round(HF.grid.frame[0] * HF.H), h: Math.round(HF.W * 9 / 16 / 2) * 2 };
  HF.dataBox = { top: HF.band.top + HF.band.h, bottom: Math.round(HF.grid.data[1] * HF.H),
                 left: Math.round(HF.W * 0.06), right: Math.round(HF.W * (1 - HF.grid.right)) };
  /**
   * 场景画在哪块画布上:横屏 = 整个画幅;竖屏「画面带」模式(HF.boot({ portrait: "band" }))
   * = 一块虚拟的 1920×1080 舞台,整体缩放塞进 16:9 画面带。车、电池包、光束这些画面元素
   * 照横屏坐标画,不用改;文字(标题、标注数字、参数条、出处)改排到下面的数据区,保持原字号。
   */
  HF.CW = HF.W; HF.CH = HF.H;
  HF.virtual = null;
  HF.vs = 1;
  /** 虚拟舞台坐标 → 页面真实像素(非竖屏画面带模式时原样返回) */
  HF.vx = 0; HF.vy = 0;
  HF.toReal = (x, y) => HF.virtual ? [HF.vx + x * HF.vs, HF.vy + y * HF.vs] : [x, y];
  HF.seconds = Number(D.seconds) || 8;
  HF.rows = Array.isArray(D.rows) ? D.rows : [];
  /** 取原文里的第一个数字:"3.23 秒" → {head:"", num:3.23, dec:2, tail:" 秒"}。没有数字返回 null */
  HF.splitNumber = function (text) {
    const s = String(text == null ? "" : text).replace(/,/g, "");
    // "4D 毫米波""5G""8K" 里的一位数是名字不是参数(和 overlays.split_number 同一条规则)
    const m = s.match(/-?(?!\d[DGK](?![A-Za-z]))\d+(?:\.\d+)?/);
    if (!m) return null;
    const dec = m[0].includes(".") ? m[0].split(".")[1].length : 0;
    return { head: s.slice(0, m.index), num: parseFloat(m[0]), dec, tail: s.slice(m.index + m[0].length) };
  };

  /** 每帧回调:用属性 setter 驱动,任意顺序 seek 都会触发 */
  HF.drive = function (tl, fn, at, dur) {
    const d = { _t: 0 };
    Object.defineProperty(d, "t", { get() { return this._t; }, set(v) { this._t = v; fn(v); } });
    tl.fromTo(d, { t: 0 }, { t: dur, duration: dur, ease: "none" }, at);
  };

  HF.el = function (tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  // ======================================================================
  // 骨架启动:所有模版共用的开场
  // ======================================================================

  /**
   * 起手:把每个模版都要写一遍的那几行收在一起。
   * 模版第一行写 `const { tl, D, root, add, items, A, T, S } = HF.boot();` 就行。
   *
   * - tl    暂停的 gsap 时间轴(结尾记得 HF.register(tl))
   * - D     注入的数据(window.__DATA__)
   * - root  根容器
   * - add   往 root 末尾挂元素
   * - items 前几条数据(通用模版用;数量不同就 HF.boot({ items: 5 }))
   * - A/T/S 节奏:总时长 S、缩放后的时间 T(x)、出场点 outAt/outDur
   *
   * 场景模版用 HF.add() 挂元素(插在颗粒层之前),跟这里的 add(挂到末尾)不是一回事,
   * 场景模版自己调 HF.add,别用这里返回的 add。
   */
  HF.boot = function (opt) {
    opt = opt || {};
    const D = window.__DATA__ || {};
    const root = document.getElementById("root");
    HF._tl = gsap.timeline({ paused: true });
    if (HF.portrait && opt.portrait === "band" && !HF.virtual) bandStage(root, opt.focus);
    const A = HF.acts();
    return {
      tl: HF._tl,
      D, root, A,
      T: A.T, S: A.S,
      add: (el) => root.appendChild(el),
      items: HF.items(opt.items || 4),
    };
  };

  /** 英文字符按 0.6em、汉字按 1em 估宽,用来给大数字挑字号 */
  function textW(s) {
    let w = 0;
    for (const ch of String(s)) w += ch.charCodeAt(0) > 255 ? 1 : 0.6;
    return w;
  }

  /** 大数字的字号:数字部分按 Montserrat、单位按 0.3 倍字号估宽,装进 avail 像素 */
  HF.fitSize = function (value, avail, maxSize) {
    const p = HF.splitNumber(value);
    if (!p) return Math.max(46, Math.min(maxSize * 0.62, avail / (textW(value) * 1.05)));
    const numChars = String(p.num.toFixed(p.dec)).length;
    const em = numChars * 0.62 + textW(p.head + p.tail) * 0.3 + 0.12;
    return Math.max(52, Math.min(maxSize, avail / em));
  };

  /** 滚动 HF.bigNumber 返回的数字;滚完停在原文数字 */
  HF.countHero = function (tl, h, at, dur, opt) {
    opt = opt || {};
    if (h.whole) return;
    const dec = h.p.dec, to = h.p.num, from = opt.from == null ? 0 : opt.from;
    const o = { v: from };
    h.count.textContent = from.toFixed(dec);
    tl.to(o, {
      v: to, duration: dur, ease: opt.ease || "expo.out",
      onUpdate: () => { h.count.textContent = o.v.toFixed(dec); },
      onComplete: () => { h.count.textContent = h.final; },
    }, at);
  };

  /** SVG 元素 */
  HF.svg = function (tag, attrs, parent) {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach((k) => e.setAttribute(k, attrs[k]));
    if (parent) parent.appendChild(e);
    return e;
  };

  /** 固定种子的伪随机(不用 Math.random,每次渲染一致) */
  HF.rng = function (seed) {
    let s = seed >>> 0;
    return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
  };

  // ======================================================================
  // 版式组件(实车图 + 标注)
  // ======================================================================

  /** 节奏:短拍压紧入场,长拍入场照常、多出来的时间交给停留段的二级运动。退场留在最后 */
  HF.acts = function () {
    const S = HF.seconds;
    const K = Math.max(0.4, Math.min(1, S / 7));
    const outDur = Math.max(0.3, Math.min(0.55, S * 0.08));
    return { S, K, T: (x) => x * K, outAt: S - outDur, outDur };
  };

  const UNIT_RE = /^\s*(kWh|kW|km\/h|km|mm|cm|TOPS|Hz|kg|N·m|Nm|V|A|L|s|秒|公里|千米|分钟|小时|度|万元|元|英寸|%|个|颗|线|匹|座|C(?![A-Za-z一-龥]))/i;

  /** 原文拆成 头 + 数字 + 单位 + 余下的说明("752V碳化硅" → 752 / V / 碳化硅)。没有数字返回 null */
  HF.parts = function (text) {
    const p = HF.splitNumber(text);
    if (!p) return null;
    const m = p.tail.match(UNIT_RE);
    const unit = m ? m[1] : "";
    const tag = (m ? p.tail.slice(m[0].length) : p.tail).trim();
    return Object.assign({}, p, { unit, tag, head: p.head.trim() });
  };

  /**
   * 大数字:[头] 数字 单位 [说明标签]。返回能交给 HF.countHero 的对象。
   * 没有数字的原文(如"全系标配")整段当大字。
   */
  HF.bigNumber = function (host, value, size, opt) {
    opt = opt || {};
    host.innerHTML = "";
    host.classList.add("bn");
    const p = HF.parts(value);
    if (!p) {
      const t = HF.el("div", "bn-txt", String(value));
      t.style.fontSize = Math.round(size) + "px";   // fitSize 对纯文字已按 0.62 缩过
      host.appendChild(t);
      return { count: t, final: String(value), whole: true };
    }
    if (p.head) { const h = HF.el("div", "bn-unit", p.head); h.style.fontSize = Math.round(size * 0.26) + "px"; host.appendChild(h); }
    const n = HF.el("div", "bn-num", p.num.toFixed(p.dec));
    n.style.fontSize = size + "px";
    host.appendChild(n);
    if (p.unit) { const u = HF.el("div", "bn-unit", p.unit); u.style.fontSize = Math.round(size * (opt.unitScale || 0.3)) + "px"; host.appendChild(u); }
    if (p.tag) { const t = HF.el("div", "bn-tag", p.tag); t.style.fontSize = Math.max(20, Math.round(size * 0.17)) + "px"; host.appendChild(t); }
    return { count: n, p, final: p.num.toFixed(p.dec), whole: false };
  };

  /** 全屏 SVG 层(引线画在这里),插在颗粒层之前 */
  HF.overlay = function () {
    let sv = document.getElementById("ov");
    if (sv) return sv;
    sv = HF.svg("svg", { id: "ov", width: HF.CW, height: HF.CH, viewBox: `0 0 ${HF.CW} ${HF.CH}` });
    if (HF.virtual) { HF.virtual.appendChild(sv); return sv; }
    const root = document.getElementById("root");
    const g = document.getElementById("grain");
    if (g) root.insertBefore(sv, g); else root.appendChild(sv);
    return sv;
  };

  /**
   * 竖屏画面带模式:建虚拟舞台(底也搬进去)和数据区面板。
   * 面板 = 标题行 + 一排条目(标注 / 参数条)+ 右下角出处。
   */
  function bandStage(root, focus) {
    HF.CW = 1920; HF.CH = 1080;
    // focus = 虚拟舞台上真正有画面的那块 [x0, y0, x1, y1](横屏的标题、脚注那片空地不要),
    // 放大到恰好装进画面带并居中;不给就整个舞台装进去
    const f = focus || [0, 0, HF.CW, HF.CH];
    HF.vs = Math.min(HF.W / (f[2] - f[0]), HF.band.h / (f[3] - f[1]));
    HF.vx = (HF.W - (f[2] - f[0]) * HF.vs) / 2 - f[0] * HF.vs;
    HF.vy = HF.band.top + (HF.band.h - (f[3] - f[1]) * HF.vs) / 2 - f[1] * HF.vs;
    // 裁在画面带里:实拍底、镜头推近都不溢出到标题区和数据区
    const clip = HF.el("div"); clip.id = "vclip";
    Object.assign(clip.style, { position: "absolute", left: "0px", top: HF.band.top + "px",
      width: HF.W + "px", height: HF.band.h + "px", overflow: "hidden" });
    const v = HF.el("div"); v.id = "virt";
    Object.assign(v.style, { position: "absolute", left: "0px", top: "0px",
      width: HF.CW + "px", height: HF.CH + "px", transformOrigin: "0 0",
      transform: "translate(" + HF.vx + "px, " + (HF.vy - HF.band.top) + "px) scale(" + HF.vs + ")" });
    const bgw = document.getElementById("bgwrap");
    if (bgw) v.appendChild(bgw);
    clip.appendChild(v);
    root.insertBefore(clip, root.firstChild);
    HF.virtual = v;
    const b = HF.dataBox;
    const dp = HF.el("div", "dp"); dp.id = "dpanel";
    Object.assign(dp.style, { left: b.left + "px", top: (b.top + 18) + "px", width: (b.right - b.left) + "px",
      height: (b.bottom - b.top - 26) + "px" });
    dp.appendChild(HF.el("div", "dp-head"));
    dp.appendChild(HF.el("div", "dp-items"));
    root.appendChild(dp);
    HF.panel = dp;
    // 条目多(尺寸镜 4 个数)时整行等比缩到面板宽度内;排版完才量得准,所以每帧量一次
    const row = dp.querySelector(".dp-items");
    HF.drive(HF._tl, () => {
      row.style.transform = "";
      const k = Math.min(1, (b.right - b.left) / Math.max(1, row.scrollWidth));
      row.style.transform = k < 1 ? "scale(" + k.toFixed(4) + ")" : "";
    }, 0, HF.seconds);
  }

  /** 数据区面板里的条目行(竖屏画面带模式) */
  function panelItems() { return HF.panel.querySelector(".dp-items"); }

  /**
   * 竖屏画面带模式:往数据区放一个「标签 + 大数字」条目(第一个大、其余小),返回
   * { b, lab, val, big }(和场景自己的数值框同形,时间线照旧排)。
   */
  HF.panelItem = function (label, value, opt) {
    opt = opt || {};
    const host = panelItems();
    const first = !host.children.length;
    const it = HF.el("div", "dp-it" + (first ? " main" : ""));
    const lab = HF.el("div", "co-lab", label || "");
    const val = HF.el("div", "co-val");
    it.appendChild(lab); it.appendChild(val);
    host.appendChild(it);
    const big = HF.bigNumber(val, value, HF.fitSize(value, first ? 420 : 270, first ? 108 : 70), { unitScale: opt.unitScale || 0.36 });
    return { b: it, lab, val, big };
  };

  /** 竖屏画面带模式下的真实坐标 SVG 层(标注引线从画面带拉到数据区) */
  function realOverlay() {
    let sv = document.getElementById("ovr");
    if (sv) return sv;
    sv = HF.svg("svg", { id: "ovr", width: HF.W, height: HF.H, viewBox: "0 0 " + HF.W + " " + HF.H });
    sv.style.position = "absolute"; sv.style.left = "0px"; sv.style.top = "0px"; sv.style.overflow = "visible";
    const root = document.getElementById("root");
    const g = document.getElementById("grain");
    if (g) root.insertBefore(sv, g); else root.appendChild(sv);
    return sv;
  }

  /** 在颗粒层之前插一个元素(竖屏画面带模式:插进虚拟舞台) */
  HF.add = function (el) {
    if (HF.virtual) { HF.virtual.appendChild(el); return el; }
    const root = document.getElementById("root");
    const g = document.getElementById("grain");
    if (g) root.insertBefore(el, g); else root.appendChild(el);
    return el;
  };

  /** 颗粒层(放在最上面) */
  HF.grain = function () {
    if (document.getElementById("grain")) return;
    const g = HF.el("div"); g.id = "grain"; g.appendChild(HF.el("div"));
    document.getElementById("root").appendChild(g);
  };

  /** 标题块:眉标(车型 · 栏目)+ 大标题,标题用遮罩从下往上露出 */
  HF.header = function (tl, at, opt) {
    opt = opt || {};
    const box = HF.el("div", "hd");
    const kick = HF.el("div", "hd-kick");
    const bar = HF.el("i"); kick.appendChild(bar);
    kick.appendChild(HF.el("span", "", [D.subject, opt.kicker].filter(Boolean).join(" · ")));
    const mask = HF.el("div", "hd-mask");
    const ttl = HF.el("div", "hd-ttl", D.title || "");
    mask.appendChild(ttl);
    box.appendChild(kick); box.appendChild(mask);
    if (HF.virtual) HF.panel.querySelector(".dp-head").appendChild(box); else HF.add(box);
    tl.fromTo(bar, { scaleY: 0 }, { scaleY: 1, duration: 0.35, ease: "power2.out" }, at);
    tl.fromTo(kick.lastChild, { opacity: 0, x: -14 }, { opacity: 1, x: 0, duration: 0.4, ease: "power2.out" }, at + 0.1);
    tl.fromTo(ttl, { yPercent: 105 }, { yPercent: 0, duration: 0.6, ease: "expo.out" }, at + 0.18);
    return box;
  };

  /** 来源脚注:右上角一行小字 + 细线 */
  HF.footnote = function (tl, at) {
    if (!D.source) return null;
    const f = HF.el("div", "fn");
    f.appendChild(HF.el("i"));
    f.appendChild(HF.el("span", "", D.source));
    if (HF.virtual) HF.panel.appendChild(f); else HF.add(f);
    tl.fromTo(f, { opacity: 0 }, { opacity: 1, duration: 0.5, ease: "power1.out" }, at);
    return f;
  };

  /**
   * 引线标注。anchor = 落在图上的点,label = 标签框的锚点(side 决定标签在线的哪一侧)。
   * 线:落点 → 斜线到拐点 → 水平到标签。返回 { show(tl, at), els }
   */
  HF.callout = function (opt) {
    if (HF.virtual) return panelCallout(opt);
    const sv = HF.overlay();
    const [ax, ay] = opt.anchor, [lx, ly] = opt.label;
    const side = opt.side || (lx < ax ? "left" : "right");
    // 拐点:斜线走到标签那一行;不越过标签端点(否则引线会折回来)
    const dx = Math.abs(ly - ay) * 0.6;
    const ex = lx < ax ? Math.max(ax - dx, lx + 20) : Math.min(ax + dx, lx - 20);
    const d = `M${ax} ${ay} L${ex} ${ly} L${lx} ${ly}`;
    const g = HF.svg("g", { class: "co" }, sv);
    const line = HF.svg("path", { d, fill: "none", stroke: HF.c("ink", 0.7), "stroke-width": 2,
      pathLength: 1, "stroke-dasharray": "1 1", "stroke-dashoffset": 1 }, g);
    const ring = HF.svg("circle", { cx: ax, cy: ay, r: 10, fill: "none", stroke: HF.c("acc"), "stroke-width": 2, opacity: 0 }, g);
    const dot = HF.svg("circle", { cx: ax, cy: ay, r: 0, fill: HF.c("acc") }, g);
    const box = HF.el("div", "co-box " + side);
    box.style.top = (ly - 4) + "px";
    if (side === "left") box.style.right = (HF.CW - lx + 18) + "px"; else box.style.left = (lx + 18) + "px";
    const lab = HF.el("div", "co-lab", opt.title || "");
    const val = HF.el("div", "co-val");
    box.appendChild(lab); box.appendChild(val);
    HF.add(box);
    const big = HF.bigNumber(val, opt.value, opt.size || 64, { unitScale: 0.36 });
    box.style.transform = "translateY(-50%)";
    return {
      els: [g, box], box, big,
      show(tl, at, countDur) {
        tl.fromTo(dot, { attr: { r: 0 } }, { attr: { r: 5 }, duration: 0.25, ease: "back.out(3)" }, at);
        tl.fromTo(ring, { attr: { r: 6 }, opacity: 1 }, { attr: { r: 30 }, opacity: 0, duration: 0.8, ease: "power2.out", immediateRender: false }, at);
        tl.fromTo(line, { attr: { "stroke-dashoffset": 1 } }, { attr: { "stroke-dashoffset": 0 }, duration: 0.5, ease: "power2.inOut" }, at + 0.05);
        tl.fromTo(lab, { opacity: 0, x: side === "left" ? 16 : -16 }, { opacity: 1, x: 0, duration: 0.4, ease: "power2.out" }, at + 0.45);
        tl.fromTo(val, { opacity: 0, y: 18 }, { opacity: 1, y: 0, duration: 0.45, ease: "power3.out" }, at + 0.5);
        HF.countHero(tl, big, at + 0.5, countDur || 0.9);
      },
      /** 停留段:落点呼吸 */
      breathe(tl, from, to) {
        const period = 1.6;
        for (let t = from; t + 0.1 < to; t += period) {
          tl.fromTo(ring, { attr: { r: 6 }, opacity: 0.9 }, { attr: { r: 26 }, opacity: 0, duration: Math.min(period, to - t), ease: "power1.out", immediateRender: false }, t);
        }
      },
    };
  };

  /**
   * 竖屏画面带模式的标注:数字排进数据区(第一条大、其余小),引线从画面带里的落点
   * 竖直落下再拐到条目上。条目位置要等排版(字体加载)后才准,所以引线每帧按条目的
   * 实际位置重算(HF.drive 的 setter 在任意 seek 下都会跑)。
   */
  function panelCallout(opt) {
    const sv = realOverlay();
    const pt = HF.toReal(opt.anchor[0], opt.anchor[1]);
    const ax = pt[0], ay = pt[1];
    const host = panelItems();
    const first = !host.children.length;
    const it = HF.el("div", "dp-it" + (first ? " main" : ""));
    const lab = HF.el("div", "co-lab", opt.title || "");
    const val = HF.el("div", "co-val");
    it.appendChild(lab); it.appendChild(val);
    host.appendChild(it);
    const big = HF.bigNumber(val, opt.value, HF.fitSize(opt.value, first ? 420 : 270, first ? 108 : 70), { unitScale: 0.36 });
    const g = HF.svg("g", { class: "co" }, sv);
    const line = HF.svg("path", { fill: "none", stroke: HF.c("ink", 0.6), "stroke-width": 2,
      pathLength: 1, "stroke-dasharray": "1 1", "stroke-dashoffset": 1 }, g);
    const ring = HF.svg("circle", { cx: ax, cy: ay, r: 10, fill: "none", stroke: HF.c("acc"), "stroke-width": 2, opacity: 0 }, g);
    const dot = HF.svg("circle", { cx: ax, cy: ay, r: 0, fill: HF.c("acc") }, g);
    const route = () => {
      const r = it.getBoundingClientRect(), root = document.getElementById("root").getBoundingClientRect();
      const ex = r.left - root.left + 14, ey = r.top - root.top - 12;
      line.setAttribute("d", "M" + ax + " " + ay + " L" + ax + " " + ey + " L" + ex + " " + ey + " L" + ex + " " + (ey + 8));
    };
    route();
    HF.drive(HF._tl, route, 0, HF.seconds);
    return {
      els: [g, it], box: it, big,
      show(tl, at, countDur) {
        tl.fromTo(dot, { attr: { r: 0 } }, { attr: { r: 5 }, duration: 0.25, ease: "back.out(3)" }, at);
        tl.fromTo(ring, { attr: { r: 6 }, opacity: 1 }, { attr: { r: 30 }, opacity: 0, duration: 0.8, ease: "power2.out", immediateRender: false }, at);
        tl.fromTo(line, { attr: { "stroke-dashoffset": 1 } }, { attr: { "stroke-dashoffset": 0 }, duration: 0.5, ease: "power2.inOut" }, at + 0.05);
        tl.fromTo(lab, { opacity: 0, y: 12 }, { opacity: 1, y: 0, duration: 0.4, ease: "power2.out" }, at + 0.45);
        tl.fromTo(val, { opacity: 0, y: 18 }, { opacity: 1, y: 0, duration: 0.45, ease: "power3.out" }, at + 0.5);
        HF.countHero(tl, big, at + 0.5, countDur || 0.9);
      },
      breathe(tl, from, to) {
        const period = 1.6;
        for (let t = from; t + 0.1 < to; t += period) {
          tl.fromTo(ring, { attr: { r: 6 }, opacity: 0.9 }, { attr: { r: 26 }, opacity: 0, duration: Math.min(period, to - t), ease: "power1.out", immediateRender: false }, t);
        }
      },
    };
  }

  /**
   * 影棚 + 车:暗色影棚底、地台光、地平线;有车图时把车放在 (cx, floorY),宽 CW。
   * 返回 rig:{ stage, car, box, floorY, P, at(u, v) → 页面像素, xray(tl, at, dur), roofFront(), cabin() }
   * 没有车图时 car 为 null,at() 按一辆"虚拟车"的外框换算,模板照样能定位。
   */
  HF.carRig = function (tl, opt) {
    opt = opt || {};
    const P = D.photo;
    const CW = opt.width || 1120, cx = opt.cx == null ? 1000 : opt.cx;
    const floorY = opt.floorY || 545;
    const bgw = document.getElementById("bgwrap");
    if (bgw) bgw.classList.add("studio");
    const stage = HF.add(HF.el("div")); stage.id = "stage";
    stage.style.transformOrigin = `${cx}px ${floorY}px`;
    const pool = HF.el("div", "studio-pool"); pool.style.left = (cx - 750) + "px"; pool.style.top = (floorY - 130) + "px";
    const hz = HF.el("div", "studio-horizon"); hz.style.top = (floorY + 4) + "px";
    stage.appendChild(pool); stage.appendChild(hz);
    const rig = { stage, floorY, P, CW, cx, car: null, img: null, edges: null };
    // 虚拟车:没有车图时的外框(宽 CW,高 0.34 CW,地线 0.95)
    const geo = P || { width: 100, height: 34, floor: 0.95, roof: 0.05, wheels: [0.239, 0.771], body: [0.02, 0.98],
                       facing: "right", profile: [] };
    const ch = CW * geo.height / geo.width;
    const box = { left: cx - CW / 2, top: floorY - geo.floor * ch, w: CW, h: ch };
    rig.box = box; rig.geo = geo;
    rig.at = (u, v) => [box.left + u * CW, box.top + v * ch];
    rig.front = geo.facing === "left" ? geo.body[0] : geo.body[1];
    rig.rear = geo.facing === "left" ? geo.body[1] : geo.body[0];
    /** 车顶前沿(激光雷达、前挡上沿):从车头往回找,第一处车顶高度接近最高点的位置 */
    rig.roofFront = () => {
      const pr = geo.profile || [];
      if (!pr.length) return rig.at(geo.facing === "left" ? 0.38 : 0.62, geo.roof + 0.02);
      const top = Math.min(...pr);
      const idx = [...pr.keys()];
      if (geo.facing !== "left") idx.reverse();
      const k = idx.find((i) => pr[i] <= top + 0.035);
      const u = geo.body[0] + (geo.body[1] - geo.body[0]) * k / (pr.length - 1);
      return rig.at(u, pr[k]);
    };
    /** 座舱中心(芯片):两轮中点,车顶到地线之间 45% 处 */
    rig.cabin = () => rig.at((geo.wheels[0] + geo.wheels[1]) / 2, geo.roof + (geo.floor - geo.roof) * 0.45);
    tl.fromTo(bgw, { opacity: 0 }, { opacity: 1, duration: 0.5, ease: "power1.out" }, 0);
    tl.fromTo([pool, hz], { opacity: 0 }, { opacity: 1, duration: 0.8, ease: "power1.out" }, 0.15);
    if (!P) {
      if (opt.ghost) rig.ghost = ghostCar(tl, stage, box, ch, opt.inAt || 0.1, opt.inDur || 1.2);
      return rig;
    }

    const car = HF.el("div", "car");
    Object.assign(car.style, { left: box.left + "px", top: box.top + "px", width: CW + "px", height: ch + "px" });
    const refl = HF.el("img", "reflect"); refl.src = P.src;
    refl.style.top = (2 * P.floor * ch - ch) + "px";
    const img = HF.el("img"); img.src = P.src;
    car.appendChild(refl); car.appendChild(img);
    let edges = null;
    if (P.edges) { edges = HF.el("div", "edges-wrap"); edges.appendChild(HF.el("div", "edges")); edges.style.clipPath = "inset(0 100% 0 0)"; car.appendChild(edges); }
    stage.appendChild(car);
    Object.assign(rig, { car, img, edges });
    // 车从车头方向的反面滑入
    const dir = geo.facing === "left" ? -1 : 1;
    tl.fromTo(car, { x: -300 * dir, opacity: 0, filter: "blur(10px)" },
                   { x: 0, opacity: 1, filter: "blur(0px)", duration: opt.inDur || 1.0, ease: "expo.out" }, opt.inAt || 0.05);

    /** X 光:扫描光从左到右,扫过处车身变暗、线稿亮起。dim = 最后车身留多少 */
    rig.xray = (tl2, at, dur, dim) => {
      dim = dim == null ? 0.2 : dim;
      const scan = HF.el("div", "scan");
      Object.assign(scan.style, { top: (box.top - 20) + "px", height: (ch + 40) + "px", left: "0px" });
      stage.appendChild(scan);
      tl2.fromTo(scan, { x: box.left - 10, opacity: 0 }, { x: box.left + CW + 10, duration: dur, ease: "power1.inOut" }, at);
      tl2.to(scan, { opacity: 1, duration: 0.12 }, at);
      tl2.to(scan, { opacity: 0, duration: 0.2 }, at + dur - 0.15);
      const pct = { v: 0 };
      const paint = () => {
        if (edges) edges.style.clipPath = `inset(0 ${100 - pct.v}% 0 0)`;
        img.style.opacity = String(1 - (1 - dim) * pct.v / 100);
      };
      tl2.to(pct, { v: 100, duration: dur, ease: "power1.inOut", onUpdate: paint }, at);
    };
    return rig;
  };

  /**
   * 没有车图时的示意车:一条通用侧影线稿(车头朝右),按虚拟车外框(wheels 0.239 / 0.771)缩放。
   * 用线画出来,不填色,明确是示意。
   */
  function ghostCar(tl, stage, box, ch, at, dur) {
    const CW = box.w;
    const mx = (x) => box.left + (0.02 + (x - 50) / 722 * 0.96) * CW;
    const my = (y) => box.top + (0.05 + (y - 102) / 260 * 0.9) * ch;
    const map = (d) => d.replace(/(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)/g, (m, x, y) => mx(+x).toFixed(1) + " " + my(+y).toFixed(1));
    const sv = HF.svg("svg", { class: "ghost", width: HF.CW, height: HF.CH, viewBox: `0 0 ${HF.CW} ${HF.CH}` }, stage);
    const line = { fill: "none", stroke: HF.c("ink"), "stroke-width": 3, "stroke-linejoin": "round", pathLength: 1,
                   "stroke-dasharray": "1 1", "stroke-dashoffset": 1 };
    const body = HF.svg("path", Object.assign({ d: map("M50 300 L50 250 Q50 228 92 222 L222 196 Q292 110 420 102 L560 102 Q650 116 704 200 L752 218 Q772 226 772 258 L772 300 Z") }, line), sv);
    const win = HF.svg("path", Object.assign({ d: map("M268 196 Q320 128 430 122 L552 122 Q620 134 664 196 Z") }, line, { "stroke-width": 2, stroke: HF.c("ink", 0.55) }), sv);
    const wheels = [215, 615].map((x) => HF.svg("ellipse", { cx: mx(x), cy: my(308), rx: 54 / 722 * 0.96 * CW, ry: 54 / 260 * 0.9 * ch,
      fill: HF.c("bg"), stroke: HF.c("ink"), "stroke-width": 3, opacity: 0 }, sv));
    tl.to(body, { attr: { "stroke-dashoffset": 0 }, duration: dur, ease: "power2.inOut" }, at);
    tl.to(win, { attr: { "stroke-dashoffset": 0 }, duration: dur * 0.7, ease: "power2.inOut" }, at + dur * 0.3);
    tl.fromTo(wheels, { opacity: 0 }, { opacity: 1, duration: 0.4 }, at + dur * 0.6);
    return sv;
  }

  /**
   * 次要参数:右侧竖排的小条目(细竖线 + 标签 + 数字),依次滑入。返回元素数组。
   * opt = { top, right, width(每条), size, row(横排), from, to(时间段) }
   */
  HF.chips = function (tl, rows, opt) {
    opt = opt || {};
    if (!rows.length) return [];
    if (HF.virtual) return panelChips(tl, rows, opt);
    const box = HF.add(HF.el("div", "chips" + (opt.row ? " row" : "")));
    box.style.top = (opt.top || 300) + "px";
    box.style.right = (opt.right == null ? 110 : opt.right) + "px";
    if (!opt.row) box.style.width = (opt.width || 380) + "px";
    const out = [];
    rows.forEach((r, n) => {
      const it = HF.el("div", "it"); const bar = HF.el("i");
      const lab = HF.el("div", "co-lab", r.label); const val = HF.el("div");
      if (opt.row) it.style.width = (opt.width || 380) + "px";
      it.appendChild(bar); it.appendChild(lab); it.appendChild(val);
      // 口径 / 来源小字("CLTC · 标准版 | 官方"),跟着这一条一起滑入
      const tag = r.tag ? HF.el("div", "co-tag", r.tag) : null;
      if (tag) it.appendChild(tag);
      box.appendChild(it);
      const big = HF.bigNumber(val, r.value, HF.fitSize(r.value, (opt.width || 380) - 40, opt.size || 64), { unitScale: 0.4 });
      const span = Math.max(0, (opt.to || opt.from) - opt.from);
      const at = opt.from + (rows.length > 1 ? span * n / (rows.length - 1) : 0);
      tl.fromTo(bar, { scaleY: 0 }, { scaleY: 1, duration: 0.35, ease: "power2.out" }, at);
      tl.fromTo([lab, val, tag].filter(Boolean), { opacity: 0, x: 24 }, { opacity: 1, x: 0, duration: 0.45, ease: "power3.out", stagger: 0.08 }, at + 0.1);
      HF.countHero(tl, big, at + 0.15, 0.8);
      out.push(it);
    });
    return out;
  };

  /** 竖屏画面带模式的次要参数:和标注排在同一行数据区里 */
  function panelChips(tl, rows, opt) {
    const host = panelItems();
    const out = [];
    rows.forEach((r, n) => {
      const it = HF.el("div", "dp-it chip"); const bar = HF.el("i");
      const lab = HF.el("div", "co-lab", r.label); const val = HF.el("div");
      it.appendChild(bar); it.appendChild(lab); it.appendChild(val);
      const tag = r.tag ? HF.el("div", "co-tag", r.tag) : null;
      if (tag) it.appendChild(tag);
      host.appendChild(it);
      const big = HF.bigNumber(val, r.value, HF.fitSize(r.value, 240, 60), { unitScale: 0.4 });
      const span = Math.max(0, (opt.to || opt.from) - opt.from);
      const at = opt.from + (rows.length > 1 ? span * n / (rows.length - 1) : 0);
      tl.fromTo(bar, { scaleY: 0 }, { scaleY: 1, duration: 0.35, ease: "power2.out" }, at);
      tl.fromTo([lab, val, tag].filter(Boolean), { opacity: 0, y: 16 }, { opacity: 1, y: 0, duration: 0.45, ease: "power3.out", stagger: 0.08 }, at + 0.1);
      HF.countHero(tl, big, at + 0.15, 0.8);
      out.push(it);
    });
    return out;
  }

  /** 镜头:整拍匀速慢推(ease none,保证任意一秒都在动)。layers = [{el, scale:[a,b], x:[a,b], y:[a,b]}] */
  HF.camera = function (tl, layers) {
    layers.forEach((L) => {
      if (!L.el) return;
      const from = {}, to = { duration: HF.seconds, ease: "none" };
      if (L.scale) { from.scale = L.scale[0]; to.scale = L.scale[1]; }
      if (L.x) { from.x = L.x[0]; to.x = L.x[1]; }
      if (L.y) { from.y = L.y[0]; to.y = L.y[1]; }
      tl.fromTo(L.el, from, to, 0);
    });
  };

  /** 退场:叠加层按顺序上飘 + 模糊消失(底保持到最后一帧) */
  HF.exit = function (tl, els, at, dur) {
    els = els.filter(Boolean);
    const step = Math.min(0.06, dur / Math.max(2, els.length * 2));
    els.forEach((el, n) => {
      tl.to(el, { opacity: 0, y: "-=18", filter: "blur(8px)", duration: Math.max(0.2, dur - n * step), ease: "power2.in" }, at + n * step);
    });
  };

  // ======================================================================
  // 通用模版(motion/templates/*.html)用的小工具:模版不认场景,只认 rows
  // ======================================================================

  HF.kicker = D.kicker || "";
  HF.cols = Array.isArray(D.columns) ? D.columns : [];
  /** 对比数据:有两列名、至少一行有两个值 */
  HF.cmp = HF.cols.length >= 2 && HF.rows.some((r) => Array.isArray(r.values) && r.values.length >= 2 && r.values[1]);
  /** 前 max 行,统一成 {label, value, b(对比的第二个值), tag} */
  HF.items = function (max) {
    return HF.rows.slice(0, max || 4).map((r) => ({
      label: String(r.label || ""), value: String(r.value == null ? "" : r.value),
      b: HF.cmp && Array.isArray(r.values) && r.values[1] ? String(r.values[1]) : "", tag: String(r.tag || ""),
    }));
  };
  HF.textW = textW;
  /** 一行文字装进 avail 像素的字号。英文按 0.6em × k 估宽(k 修正这款西文字体的宽窄),汉字按 1em */
  HF.fit = function (text, avail, max, k) {
    let w = 0;
    for (const ch of String(text)) w += ch.charCodeAt(0) > 255 ? 1 : 0.6 * (k || 1);
    return Math.max(18, Math.min(max, avail / Math.max(0.6, w)));
  };
  /** 文字里的第一个数字从 0 滚到原值;滚完写回原文一字不差。没有数字直接写原文 */
  HF.countText = function (tl, el, text, at, dur, opt) {
    text = String(text == null ? "" : text);
    const p = HF.splitNumber(text);
    if (!p || !(dur > 0)) { el.textContent = text; return; }
    const o = { v: (opt && opt.from) || 0 };
    const fmt = (v) => p.head + v.toFixed(p.dec) + p.tail;
    el.textContent = fmt(o.v);
    tl.to(o, { v: p.num, duration: dur, ease: (opt && opt.ease) || "expo.out",
      onUpdate: () => { el.textContent = fmt(o.v); }, onComplete: () => { el.textContent = text; } }, at);
  };
  /** 逐字拆成 span(汉字、字母各一个),返回 span 数组 */
  HF.chars = function (host, text, cls) {
    host.textContent = "";
    return Array.from(String(text)).map((ch) => {
      const s = HF.el("span", cls || "ch", ch === " " ? " " : ch);
      s.style.display = "inline-block";
      host.appendChild(s);
      return s;
    });
  };
  /** 打字机:at 开始按 cps 字/秒打出 text;返回打完的时刻 */
  HF.type = function (tl, el, text, at, cps) {
    text = String(text);
    const n = Array.from(text).length, dur = Math.max(0.05, n / (cps || 28));
    const chars = Array.from(text);
    el.textContent = "";
    HF.drive(tl, (t) => { el.textContent = chars.slice(0, Math.min(n, Math.ceil(n * t / dur))).join(""); }, at, dur);
    return at + dur;
  };
  /** 把 [from, to] 均分给 n 段,返回每段起点 */
  HF.slots = function (n, from, to) {
    return Array.from({ length: n }, (_, k) => from + (n > 1 ? (to - from) * k / n : 0));
  };
  /** 车图元素(通用模版用;没有车图返回 null) */
  HF.photoImg = function (cls) {
    if (!D.photo) return null;
    const im = HF.el("img", cls || "");
    im.src = D.photo.src;
    return im;
  };


  // ======================================================================
  // 大制作模版用:分镜时间、解码文字、three.js 舞台
  // ======================================================================

  /**
   * 分镜:开场 → 每个条目一个镜头 → 收尾全景。返回 {intro:[a,b], shots:[[a,b]...], outro:[a,b], S}。
   * 短拍压缩开场和收尾,条目镜头至少 0.9 秒;收尾段里包含退场。
   */
  HF.plan = function (n, opt) {
    opt = opt || {};
    const S = HF.seconds;
    n = Math.max(1, n);
    let intro = Math.max(0.6, Math.min(opt.intro || 1.6, S * 0.17));
    let outro = Math.max(0.7, Math.min(opt.outro || 1.9, S * 0.2));
    if (S - intro - outro < n * 0.9) { const k = Math.max(0.3, (S - n * 0.9) / (intro + outro)); intro *= Math.min(1, k); outro *= Math.min(1, k); }
    const L = (S - intro - outro) / n;
    const shots = Array.from({ length: n }, (_, k) => [intro + k * L, intro + (k + 1) * L]);
    return { S, intro: [0, intro], shots, outro: [S - outro, S], L };
  };

  /** 缓动函数(给逐帧驱动用) */
  HF.ease = function (name) { return gsap.parseEase(name || "power2.inOut"); };
  HF.clamp01 = (x) => Math.max(0, Math.min(1, x));
  /** t 在 [a, b] 里的进度(0..1) */
  HF.prog = (t, a, b) => HF.clamp01((t - a) / Math.max(1e-6, b - a));
  HF.lerp = (a, b, f) => a + (b - a) * f;

  const GLYPHS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ#%&*+=<>/";
  /** 解码:乱码逐字落定成原文(从左到右),结束一字不差。汉字位置用方块乱码 */
  HF.scramble = function (tl, el, text, at, dur, seed) {
    text = String(text == null ? "" : text);
    const chars = Array.from(text), n = chars.length, rng = HF.rng(seed || 11);
    const noise = Array.from({ length: 24 }, () => chars.map((c) => c === " " ? " " :
      c.charCodeAt(0) > 255 ? "▓▒░█"[Math.floor(rng() * 4)] : GLYPHS[Math.floor(rng() * GLYPHS.length)]));
    el.textContent = "";
    HF.drive(tl, (t) => {
      if (t >= dur) { el.textContent = text; return; }
      const f = t / dur, done = Math.floor(f * f * (n + 1)), frame = noise[Math.floor(t * 30) % noise.length];
      el.textContent = chars.map((c, i) => i < done ? c : (i < done + 6 ? frame[i] : "")).join("");
    }, at, dur + 0.001);
  };

  /**
   * three.js 舞台:画布铺满整个画幅(HF.W×HF.H)插在 root 最底层。返回 {THREE, renderer, scene, camera, canvas, render(), project(v)}。
   * 所有动画都在 HF.drive 回调里按时间算好再 render(),seek 到任意时刻画面一致。
   */
  HF.stage3d = function (opt) {
    opt = opt || {};
    const T3 = window.THREE;
    const renderer = new T3.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, alpha: !!opt.alpha });
    renderer.setPixelRatio(1);
    renderer.setSize(HF.W, HF.H, false);
    renderer.outputColorSpace = T3.SRGBColorSpace;
    renderer.toneMapping = T3.ACESFilmicToneMapping;
    renderer.toneMappingExposure = opt.exposure || 1.0;
    const cv = renderer.domElement;
    cv.style.cssText = `position:absolute;left:0;top:0;width:${HF.W}px;height:${HF.H}px;`;
    const root = document.getElementById("root");
    root.insertBefore(cv, root.firstChild ? root.firstChild.nextSibling : null);
    const scene = new T3.Scene();
    if (opt.bg != null) scene.background = new T3.Color(opt.bg);
    if (opt.fog) scene.fog = new T3.FogExp2(opt.fog[0], opt.fog[1]);
    const camera = new T3.PerspectiveCamera(opt.fov || 35, HF.W / HF.H, 0.1, opt.far || 400);
    const v = new T3.Vector3();
    const st = {
      THREE: T3, renderer, scene, camera, canvas: cv,
      render() { renderer.render(scene, camera); },
      /** 世界坐标 → 页面像素 [x, y, 在镜头前] */
      project(p) { v.copy(p).project(camera); return [(v.x + 1) * HF.W / 2, (1 - v.y) * HF.H / 2, v.z < 1]; },
    };
    return st;
  };

  /** 车图做成 three.js 贴图(等图片加载完再上传);没有车图返回 null */
  HF.photoTexture = function () {
    if (!D.photo || !window.THREE) return null;
    const img = HF.el("img"); img.src = D.photo.src; img.style.display = "none";
    document.body.appendChild(img);
    const tex = new THREE.Texture(img);
    tex.colorSpace = THREE.SRGBColorSpace;
    tex.anisotropy = 8;
    let ok = false;
    tex.ready = () => { if (!ok && img.complete && img.naturalWidth) { tex.needsUpdate = true; ok = true; } return ok; };
    return tex;
  };

  /** 一行文字在离屏画布上的像素点(粒子拼字用)。font 写全,如 '800 300px Montserrat'。返回 [[x,y]...] 以 (0,0) 为中心 */
  HF.textPoints = function (text, font, step) {
    const oc = document.createElement("canvas"); oc.width = 2400; oc.height = 900;
    const o = oc.getContext("2d");
    o.fillStyle = "#fff"; o.textAlign = "center"; o.textBaseline = "middle"; o.font = font;
    o.fillText(String(text), 1200, 450);
    const img = o.getImageData(0, 0, 2400, 900).data, pts = [];
    step = step || 5;
    for (let y = 0; y < 900; y += step) for (let x = 0; x < 2400; x += step)
      if (img[(y * 2400 + x) * 4 + 3] > 128) pts.push([x - 1200, 450 - y]);
    return pts;
  };

  HF.register = function (tl) {
    window.__timelines = window.__timelines || {};
    window.__timelines["main"] = tl;
    tl.seek(0);
  };
})();
