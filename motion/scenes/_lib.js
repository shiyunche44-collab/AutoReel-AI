// 概念动画模板的公用小工具(风格 A 玻璃拟态)。motion.py 渲染前把它和 gsap.min.js 一起拷进工作目录。
//
// 数据约定(motion.py 注入 window.__DATA__):
//   { subject, title, seconds, columns, rows: [{label, value, values}], source }
// rows 的 value 都是核对过事实的原文 —— 模板只负责"画",数字滚动结束时必须回到原文一字不差。
(function () {
  const D = window.__DATA__ || {};
  const HF = (window.HF = {});
  HF.data = D;
  HF.seconds = Number(D.seconds) || 8;
  HF.rows = Array.isArray(D.rows) ? D.rows : [];
  // 时间缩放:短拍(2~3 秒)把入场节奏压紧,6 秒以上按设计节奏
  HF.K = Math.max(0.45, Math.min(1, HF.seconds / 6));
  HF.t = (x) => x * HF.K;

  /** 取原文里的第一个数字:"3.23 秒" → {head:"", num:3.23, dec:2, tail:" 秒"}。没有数字返回 null */
  HF.splitNumber = function (text) {
    const s = String(text == null ? "" : text).replace(/,/g, "");
    const m = s.match(/-?\d+(?:\.\d+)?/);
    if (!m) return null;
    const dec = m[0].includes(".") ? m[0].split(".")[1].length : 0;
    return { head: s.slice(0, m.index), num: parseFloat(m[0]), dec, tail: s.slice(m.index + m[0].length) };
  };

  /** 在时间轴 `tl` 的 `at` 秒处把 el 的文字从 `from` 滚到原文 `value`。滚完写回原文 */
  HF.countUp = function (tl, el, value, at, dur, opt) {
    opt = opt || {};
    const p = HF.splitNumber(value);
    if (!p) { el.textContent = String(value); return; }
    const from = opt.from == null ? 0 : opt.from;
    const o = { v: from };
    el.textContent = p.head + from.toFixed(p.dec) + p.tail;
    tl.to(o, {
      v: p.num, duration: dur, ease: opt.ease || "expo.out",
      onUpdate: () => { el.textContent = p.head + o.v.toFixed(p.dec) + p.tail; },
      onComplete: () => { el.textContent = String(value); },
    }, at);
  };

  /** 每帧回调:用属性 setter 驱动,任意顺序 seek 都会触发 */
  HF.drive = function (tl, fn, at, dur) {
    const d = { _t: 0 };
    Object.defineProperty(d, "t", { get() { return this._t; }, set(v) { this._t = v; fn(v); } });
    tl.fromTo(d, { t: 0 }, { t: dur, duration: dur, ease: "none" }, at);
  };

  /** 进度 0–1:原文里的数字相对 `max` 的比例,只用来决定图形画多满(示意,不显示) */
  HF.ratio = function (value, max) {
    const p = HF.splitNumber(value);
    if (!p || !max) return 1;
    return Math.max(0.08, Math.min(1, p.num / max));
  };

  HF.el = function (tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
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

  /** 把大数字写进 host:数字部分单独一个 span(用来滚动),头尾当单位。返回数字 span */
  function heroInto(host, value, size) {
    const p = HF.splitNumber(value);
    host.innerHTML = "";
    if (!p) {
      const t = HF.el("div", "txt", String(value));
      t.style.fontSize = size + "px";
      host.appendChild(t);
      return { count: t, final: String(value), whole: true };
    }
    if (p.head) { const h = HF.el("div", "unit", p.head); h.style.fontSize = size * 0.3 + "px"; host.appendChild(h); }
    const n = HF.el("div", "num", p.num.toFixed(p.dec));
    n.style.fontSize = size + "px";
    host.appendChild(n);
    if (p.tail.trim()) { const u = HF.el("div", "unit", p.tail.trim()); u.style.fontSize = size * 0.3 + "px"; host.appendChild(u); }
    return { count: n, p, final: p.num.toFixed(p.dec), whole: false };
  }

  HF.heroInto = heroInto;

  /** 滚动 heroInto 返回的数字;滚完停在原文数字 */
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

  /**
   * 搭玻璃卡框架并排好入场。返回 { tl, root, card, hero, rows, ... }。
   * opt.heroInVisual:大数字画在右侧示意区里(卡上不放),卡上只列其余行。
   * opt.maxRows:卡上最多列几行小参数(默认 3)。
   */
  HF.frame = function (opt) {
    opt = opt || {};
    const root = document.getElementById("root");
    const tl = gsap.timeline({ paused: true });
    const T = HF.t;
    const out = { tl, root, K: HF.K, T };

    const shade = HF.el("div", "shade");
    const swr = HF.el("div", "sweep-rot"); const sw = HF.el("div", "sweep"); swr.appendChild(sw);
    root.appendChild(shade); root.appendChild(swr);

    const card = HF.el("div", "card glass");
    const pill = HF.el("div", "pill", D.subject || "");
    const ttl = HF.el("div", "ttl", D.title || "");
    const rule = HF.el("div", "rule");
    card.appendChild(pill); card.appendChild(ttl); card.appendChild(rule);
    if (!opt.noCard) root.appendChild(card);

    const rows = opt.noCard ? [] : HF.rows;
    const first = rows[0];
    let heroObj = null, restFrom = 0;
    if (first && !opt.heroInVisual) {
      const lab = HF.el("div", "hlab", first.label);
      const hv = HF.el("div", "hval");
      card.appendChild(lab); card.appendChild(hv);
      const size = HF.fitSize(first.value, 640, opt.heroMax || 190);
      heroObj = heroInto(hv, first.value, size);
      out.heroLab = lab; out.heroVal = hv;
      restFrom = 1;
    } else if (opt.heroInVisual) {
      restFrom = 1;
    }
    out.hero = heroObj;

    const list = HF.el("div", "rows");
    const rowEls = [];
    rows.slice(restFrom, restFrom + (opt.maxRows == null ? 3 : opt.maxRows)).forEach((r) => {
      const row = HF.el("div", "r");
      const rl = HF.el("div", "rl", r.label);
      const rv = HF.el("div", "rv");
      const size = Math.max(26, Math.min(44, 330 / textW(r.value) * 1.0));
      rv.style.fontSize = size + "px";
      row.appendChild(rl); row.appendChild(rv);
      list.appendChild(row);
      rowEls.push({ row, rv, value: r.value });
    });
    if (rowEls.length) card.appendChild(list);
    out.rows = rowEls;

    if (D.source) {
      const src = HF.el("div", "src glass", D.source);
      root.appendChild(src);
      out.src = src;
      tl.fromTo(src, { opacity: 0, y: -16 }, { opacity: 1, y: 0, duration: T(0.5), ease: "power2.out" }, T(0.9));
    }
    const grain = HF.el("div"); grain.id = "grain"; grain.appendChild(HF.el("div")); root.appendChild(grain);

    // 入场:卡从左滑入 → 胶囊 → 标题 → 分隔线展开 → 大数字滚动 → 行依次出现
    tl.fromTo(card, { opacity: 0, x: -90 }, { opacity: 1, x: 0, duration: T(0.8), ease: "expo.out" }, T(0.15));
    tl.fromTo(pill, { opacity: 0, x: -20 }, { opacity: 1, x: 0, duration: T(0.4), ease: "power2.out" }, T(0.4));
    tl.fromTo(ttl, { opacity: 0, y: 34 }, { opacity: 1, y: 0, duration: T(0.55), ease: "power3.out" }, T(0.5));
    tl.fromTo(rule, { scaleX: 0 }, { scaleX: 1, duration: T(0.8), ease: "power2.inOut" }, T(0.65));
    if (heroObj) {
      tl.fromTo(out.heroLab, { opacity: 0 }, { opacity: 1, duration: T(0.4) }, T(0.8));
      tl.fromTo(out.heroVal, { opacity: 0, y: 30 }, { opacity: 1, y: 0, duration: T(0.5), ease: "power3.out" }, T(0.8));
      HF.countHero(tl, heroObj, T(0.85), Math.max(0.9, T(1.5)));
    }
    rowEls.forEach((r, n) => {
      const at = T(1.2 + n * 0.28);
      tl.fromTo(r.row, { opacity: 0, y: 26 }, { opacity: 1, y: 0, duration: T(0.5), ease: "back.out(1.5)" }, at);
      HF.countUp(tl, r.rv, r.value, at + 0.05, Math.max(0.7, T(1.1)));
    });
    // 光扫:一条斜光带从左划过卡面
    tl.fromTo(sw, { x: -800 }, { x: 2400, duration: Math.max(1.0, T(1.8)), ease: "power1.inOut" }, T(1.8));
    // 实拍底缓慢推近
    const bg = document.getElementById("bgwrap");
    if (bg) tl.fromTo(bg, { scale: 1.0 }, { scale: 1.06, duration: HF.seconds, ease: "sine.inOut" }, 0);
    return out;
  };

  /** 右侧示意区容器,插在颗粒层之前 */
  HF.stage = function (cls) {
    const s = HF.el("div", "stage" + (cls ? " " + cls : ""));
    const g = document.getElementById("grain");
    document.getElementById("root").insertBefore(s, g);
    return s;
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

  /** 收尾:最后 0.35 秒只淡出叠加层(玻璃卡、示意区、来源),实拍底保持到最后一帧 */
  HF.outro = function (tl, sel) {
    tl.to(sel || ".card, .src, .stage, .tbl", { opacity: 0, duration: 0.35, ease: "power1.in" },
      Math.max(0.5, HF.seconds - 0.4));
  };

  HF.register = function (tl) {
    window.__timelines = window.__timelines || {};
    window.__timelines["main"] = tl;
    tl.seek(0);
  };
})();
