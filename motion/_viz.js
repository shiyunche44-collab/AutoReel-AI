// 概念动画的场景画面组件 + 动效语法。motion.py 渲染前把它和 _lib.js 一起拷进工作目录。
//
// 模版 = 视觉语言(版式、字体、配色、运镜);画面中央讲的东西由这里按场景画:
//   HF.viz.make(scene, box, items, opt) → 电池包 / 充电曲线 / 零百表 / 尺寸线 / 感知扇面 / 芯片 / 对比条
// 颜色全走模版的 CSS 变量(--acc --hi --acc2 --ink --bg --glow),用 HF.c() 取。
//
// 数据规矩:
//   - 表示数量的图形(长短、角度、亮多少格)只按 motion.py 给的 ratio / share / 真实数值画;
//     没有就只画结构(电池包、芯片的样子),不画装饰性的满格。
//   - 每行的出现时刻优先用旁白说到它的时刻 it.at(HF.beats 排),没有才均匀排。
(function () {
  const HF = window.HF;
  const D = HF.data;

  // ======================================================================
  // 数据与节奏
  // ======================================================================

  /** 前 max 行,在 HF.items 的基础上带上 at / ratio / num / unit / share */
  HF.items = function (max) {
    return HF.rows.slice(0, max || 4).map((r) => ({
      label: String(r.label || ""), value: String(r.value == null ? "" : r.value),
      b: HF.cmp && Array.isArray(r.values) && r.values[1] ? String(r.values[1]) : "", tag: String(r.tag || ""),
      at: typeof r.at === "number" ? r.at : null,
      ratio: typeof r.ratio === "number" ? r.ratio : null,
      num: typeof r.num === "number" ? r.num : null,
      unit: String(r.unit || ""),
      share: Array.isArray(r.share) ? r.share : null,
    }));
  };

  /**
   * 每行出现的时刻:有 it.at(旁白说到它)就用,没有的在 [from, to] 里均匀补位。
   * 相邻两行至少隔 gap 秒(镜头要有时间走过去),整体不晚于 to。返回与 items 同序的时刻。
   */
  HF.beats = function (items, from, to, gap) {
    gap = gap || 0.9;
    const n = items.length;
    if (!n) return [];
    const even = HF.slots(n, from, Math.max(from, to));
    const raw = items.map((it, k) => (it.at != null ? Math.max(from, Math.min(to, it.at)) : even[k]));
    // 按时刻排序后推开过近的
    const order = raw.map((t, k) => [t, k]).sort((a, b) => a[0] - b[0]);
    let last = -Infinity;
    order.forEach((o) => { o[0] = Math.max(o[0], last + gap); last = o[0]; });
    // 推过头了整体往前挪,但不早于 from
    const over = last - to;
    if (over > 0) order.forEach((o) => { o[0] = Math.max(from, o[0] - over); });
    const out = new Array(n);
    order.forEach((o) => { out[o[1]] = o[0]; });
    return out;
  };

  /**
   * studio 场景的标注时刻:前 n 行有旁白时刻(at)就跟着旁白走(最晚到 hardTo),
   * 都没有就照旧在 [from, evenTo] 里均匀铺开。
   */
  HF.coTimes = function (rows, n, from, evenTo, hardTo, gap) {
    const rs = rows.slice(0, n);
    if (!rs.some((r) => r && r.at != null)) return Array.from({ length: n }, (_, k) => (n > 1 ? from + (Math.max(from, evenTo) - from) * k / (n - 1) : from));
    return HF.beats(rs.map((r) => r || {}), from, Math.max(from, hardTo), gap || 0.8);
  };

  /** 行的角色:按单位和参数名认它是哪类参数(场景画面据此决定画在哪) */
  HF.role = function (it) {
    const L = it.label, u = it.unit;
    if (/零百|加速/.test(L) && u === "秒") return "zero";
    if (/极速|最高车速/.test(L) || u === "km/h") return "top";
    if (u === "kWh") return "cap";
    if (u === "km") return "range";
    if (u === "V" || /电压/.test(L)) return "volt";
    if (u === "kW" || /功率|马力/.test(L)) return "power";
    if (u === "分钟" || /充电|补能/.test(L)) return "time";
    if (u === "TOPS" || /算力/.test(L)) return "tops";
    if (/芯片|处理器|SoC/i.test(L)) return "chipname";
    if (/轴距/.test(L)) return "wb";
    if (/宽/.test(L)) return "wid";
    if (/高/.test(L)) return "hgt";
    if (/长/.test(L)) return "len";
    if (/雷达|摄像头|传感/.test(L) && u !== "米") return "count";
    if (u === "米" || /距离|探测/.test(L)) return "dist";
    return "other";
  };

  // ======================================================================
  // 动效语法
  // ======================================================================

  /**
   * 统一的入场。kind:
   *   wipe  从左往右擦出(clip-path)      rise  遮罩里从下往上露出
   *   blur  失焦 + 字距收拢后清晰         scale 从 0.92 放大落定
   *   draw  SVG 线条描出来(元素要有 pathLength=1)
   */
  HF.reveal = function (tl, el, kind, at, dur) {
    if (!el) return;
    dur = dur || 0.7;
    if (kind === "wipe") {
      tl.fromTo(el, { clipPath: "inset(-20% 100% -20% 0%)" }, { clipPath: "inset(-20% 0% -20% 0%)", duration: dur, ease: "expo.inOut" }, at);
    } else if (kind === "rise") {
      const p = el.parentElement;
      if (p && !p._masked) { p.style.overflow = "hidden"; p._masked = true; }
      tl.fromTo(el, { yPercent: 110 }, { yPercent: 0, duration: dur, ease: "expo.out" }, at);
    } else if (kind === "blur") {
      tl.fromTo(el, { opacity: 0, filter: "blur(18px)", letterSpacing: "0.25em" },
                    { opacity: 1, filter: "blur(0px)", letterSpacing: "0em", duration: dur, ease: "power3.out" }, at);
    } else if (kind === "scale") {
      tl.fromTo(el, { opacity: 0, scale: 0.92 }, { opacity: 1, scale: 1, duration: dur, ease: "expo.out" }, at);
    } else if (kind === "draw") {
      tl.fromTo(el, { attr: { "stroke-dashoffset": 1 } }, { attr: { "stroke-dashoffset": 0 }, duration: dur, ease: "power2.inOut" }, at);
    } else {
      tl.fromTo(el, { opacity: 0 }, { opacity: 1, duration: dur, ease: "power1.out" }, at);
    }
  };

  /**
   * 数字逐位滚动(里程表式):host 里写成 [头][逐位滚动的数字][尾]。滚完停在原文数字,一字不差。
   * 没有数字的原文直接写进去。返回 { num, unit, tag } 三个元素(没有的为 null),方便模版分别排字号。
   */
  HF.roll = function (tl, host, text, at, dur, opt) {
    opt = opt || {};
    text = String(text == null ? "" : text);
    host.textContent = "";
    const p = HF.parts(text);
    if (!p) { host.appendChild(document.createTextNode(text)); return { num: host, unit: null, tag: null }; }
    const final = p.num.toFixed(p.dec);
    const num = HF.el("span", "roll");
    num.style.display = "inline-flex";
    if (p.head) host.appendChild(HF.el("span", "roll-head", p.head + " "));
    host.appendChild(num);
    const unit = p.unit ? HF.el("span", "roll-unit", p.unit) : null;
    if (unit) host.appendChild(unit);
    const tag = p.tag ? HF.el("span", "roll-tag", p.tag) : null;
    if (tag) host.appendChild(tag);
    const digits = Array.from(final);
    const nd = digits.filter((c) => /\d/.test(c)).length;
    let di = 0;
    digits.forEach((ch) => {
      if (!/\d/.test(ch)) { num.appendChild(HF.el("span", "roll-sep", ch)); return; }
      const win = HF.el("span", "roll-win");
      // 上下边缘渐隐:滚动途中半个数字是虚的,不是生硬地切一刀
      const fade = "linear-gradient(180deg, rgba(0,0,0,0) 0%, #000 24%, #000 76%, rgba(0,0,0,0) 100%)";
      Object.assign(win.style, { display: "inline-block", height: "1em", lineHeight: "1em", overflow: "hidden", verticalAlign: "top",
                                 webkitMaskImage: fade, maskImage: fade });
      const col = HF.el("span", "roll-col");
      Object.assign(col.style, { display: "flex", flexDirection: "column" });
      // 每一位转过的圈数:高位转得少、低位转得多,像真的里程表
      const turns = 1 + Math.floor((nd - di) / 2);
      const target = Number(ch);
      const seq = [];
      for (let k = 0; k < turns * 10 + target + 1; k++) seq.push(k % 10);
      seq.forEach((d) => { const s = HF.el("span", "", String(d)); s.style.height = "1em"; col.appendChild(s); });
      win.appendChild(col);
      num.appendChild(win);
      const steps = seq.length - 1;
      tl.fromTo(col, { yPercent: 0 }, { yPercent: -100 * steps / seq.length, duration: dur || 1.1,
        ease: opt.ease || "expo.out" }, at + di * 0.04);
      // 落定后去掉渐隐,数字完整显示
      tl.set(win, { webkitMaskImage: "none", maskImage: "none" }, at + di * 0.04 + (dur || 1.1));
      di++;
    });
    return { num, unit, tag };
  };

  /**
   * 关键帧运镜:el 在每个关键帧时刻移动到 {x, y, scale}(过渡 move 秒),两帧之间不静止,
   * 继续朝下一帧方向慢漂(drift 倍的位移)。keys = [{at, x, y, scale}],第 0 帧是起始状态。
   */
  HF.shots = function (tl, el, keys, opt) {
    opt = opt || {};
    if (!el || !keys.length) return;
    const move = opt.move || 1.0;
    const k0 = keys[0];
    tl.set(el, { x: k0.x || 0, y: k0.y || 0, scale: k0.scale || 1 }, 0);
    for (let n = 1; n < keys.length; n++) {
      const k = keys[n], prev = keys[n - 1];
      // 上一帧到这一帧之间的慢漂(停留段也在动)
      const hold = Math.max(0, k.at - prev.at - (n > 1 ? move : 0));
      const from = n > 1 ? prev.at + move : prev.at;
      if (hold > 0.05) {
        tl.to(el, { scale: (prev.scale || 1) * (1 + 0.012 * hold), x: (prev.x || 0) + ((k.x || 0) - (prev.x || 0)) * 0.04,
                    y: (prev.y || 0) + ((k.y || 0) - (prev.y || 0)) * 0.04, duration: hold, ease: "none" }, from);
      }
      tl.to(el, { x: k.x || 0, y: k.y || 0, scale: k.scale || 1, duration: move, ease: opt.ease || "power3.inOut" }, k.at);
    }
    const last = keys[keys.length - 1];
    const tail = HF.seconds - (last.at + move);
    if (tail > 0.05) tl.to(el, { scale: (last.scale || 1) * (1 + 0.012 * tail), duration: tail, ease: "none" }, last.at + move);
  };

  /** 光扫:一道斜光从 host 表面掠过(host 需要 position 定位;光被裁在 host 里) */
  HF.sweep = function (tl, host, at, opt) {
    opt = opt || {};
    if (!host) return null;
    host.style.overflow = host.style.overflow || "hidden";
    const s = HF.el("div", "hf-sweep");
    const w = opt.width || 0.35;
    Object.assign(s.style, { position: "absolute", top: "-20%", bottom: "-20%", left: "0px", width: (w * 100) + "%",
      pointerEvents: "none", mixBlendMode: opt.blend || "color-dodge", transform: "skewX(-18deg)",
      background: `linear-gradient(90deg, rgba(255,255,255,0) 0%, ${opt.color || "rgba(255,255,255,0.55)"} 50%, rgba(255,255,255,0) 100%)` });
    host.appendChild(s);
    tl.fromTo(s, { left: (-w * 160) + "%", opacity: 1 }, { left: "130%", duration: opt.dur || 0.9, ease: "power2.inOut" }, at);
    tl.set(s, { opacity: 0 }, at + (opt.dur || 0.9));
    tl.set(s, { opacity: 0 }, 0);
    tl.set(s, { opacity: 1 }, at);
    return s;
  };

  // ======================================================================
  // 场景画面
  // ======================================================================

  /** 画面组件的公共壳:box = {x, y, w, h}(页面像素),里面一张同尺寸的 SVG */
  function shell(box, cls) {
    const host = HF.el("div", "viz " + (cls || ""));
    Object.assign(host.style, { position: "absolute", left: box.x + "px", top: box.y + "px", width: box.w + "px", height: box.h + "px" });
    const sv = HF.svg("svg", { width: box.w, height: box.h, viewBox: `0 0 ${box.w} ${box.h}`, overflow: "visible" });
    sv.style.position = "absolute"; sv.style.left = "0"; sv.style.top = "0"; sv.style.overflow = "visible";
    host.appendChild(sv);
    return { host, sv };
  }
  const line = (o) => Object.assign({ fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round", pathLength: 1,
                                       "stroke-dasharray": "1 1", "stroke-dashoffset": 1 }, o);
  const G = (a) => `drop-shadow(0 0 ${8 * a}px ${HF.c("acc", 0.8)})`;

  /** 小标签(SVG 里的字);字体由模版通过 opt.font 指定 */
  let FONT = "Montserrat, Noto Sans SC, sans-serif";
  function label(sv, x, y, text, o) {
    o = o || {};
    const t = HF.svg("text", { x, y, fill: o.fill || HF.c("ink", 0.7), "font-size": o.size || 22,
      "font-family": o.family || FONT, "font-weight": o.weight || 600,
      "text-anchor": o.anchor || "start", "letter-spacing": o.ls || "0.04em", opacity: o.opacity == null ? 0 : o.opacity }, sv);
    t.textContent = text;
    return t;
  }

  const SCENES = {};

  /**
   * 电池:斜放的电池包 + 模组阵列。
   *   cap(kWh)  模组按 ratio 从一端亮起 —— 同级里电量越大亮得越满;没有 ratio 就整体通电一遍,不表示多少
   *   range(km) 包下方一条"里程线",长度按 ratio
   *   volt(V)   包两端的母线电压脉冲,脉冲强度按 ratio
   */
  SCENES.battery = function (box, items, opt) {
    const { host, sv } = shell(box, "viz-battery");
    const W = box.w, H = box.h;
    const COLS = 12, ROWS = 5;
    const pw = W * 0.86, ph = H * 0.46, px = (W - pw) / 2, py = H * 0.16;
    const g = HF.svg("g", { transform: `translate(${px} ${py}) skewX(-14)` }, sv);
    const frame = HF.svg("rect", line({ x: 0, y: 0, width: pw, height: ph, rx: 18, stroke: HF.c("acc"), "stroke-width": 3 }), g);
    frame.style.filter = G(opt.glow);
    const cells = [];
    const gap = 8, cw = (pw - 32 - gap * (COLS - 1)) / COLS, chh = (ph - 32 - gap * (ROWS - 1)) / ROWS;
    for (let c = 0; c < COLS; c++) for (let r = 0; r < ROWS; r++) {
      const e = HF.svg("rect", { x: 16 + c * (cw + gap), y: 16 + r * (chh + gap), width: cw, height: chh, rx: 4,
        fill: HF.c("acc", 0), stroke: HF.c("acc", 0.35), "stroke-width": 1.5, opacity: 0 }, g);
      cells.push({ e, c, r });
    }
    // 母线
    const busL = HF.svg("line", line({ x1: -40, y1: ph / 2, x2: 0, y2: ph / 2, stroke: HF.c("hi"), "stroke-width": 4 }), g);
    const busR = HF.svg("line", line({ x1: pw, y1: ph / 2, x2: pw + 40, y2: ph / 2, stroke: HF.c("hi"), "stroke-width": 4 }), g);
    // 能量流光
    const flow = HF.svg("rect", { x: 0, y: 0, width: pw * 0.18, height: ph, fill: HF.c("hi", 0.18), opacity: 0 }, g);
    // 里程线
    const ry = py + ph + H * 0.2;
    const road = HF.svg("line", line({ x1: px, y1: ry, x2: px + pw, y2: ry, stroke: HF.c("ink", 0.25), "stroke-width": 2 }), sv);
    const run = HF.svg("line", line({ x1: px, y1: ry, x2: px + pw, y2: ry, stroke: HF.c("acc"), "stroke-width": 6 }), sv);
    const pin = HF.svg("circle", { cx: px, cy: ry, r: 0, fill: HF.c("hi") }, sv);
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => HF.svg("line", { x1: px + pw * f, y1: ry - 10, x2: px + pw * f, y2: ry + 10, stroke: HF.c("ink", 0.3), "stroke-width": 2, opacity: 0 }, sv));
    const parts = { cap: [px + pw * 0.45, py + ph * 0.5], range: [px + pw * 0.5, ry], volt: [px + pw + 20, py + ph / 2], other: [W / 2, H / 2] };

    return {
      host, parts,
      show(tl, at) {
        HF.reveal(tl, frame, "draw", at, 0.9);
        tl.to(cells.map((c) => c.e), { opacity: 1, duration: 0.3, stagger: { each: 0.006, from: "start" } }, at + 0.3);
        HF.reveal(tl, road, "draw", at + 0.5, 0.8);
        tl.to(ticks, { opacity: 1, duration: 0.3, stagger: 0.05 }, at + 0.8);
      },
      hit(tl, it, at) {
        const role = HF.role(it);
        if (role === "cap") {
          const lit = it.ratio == null ? cells.length : Math.max(1, Math.round(it.ratio * cells.length));
          // 按列点亮:电量从一端涨上来
          cells.slice().sort((a, b) => a.c - b.c || a.r - b.r).forEach((c, n) => {
            if (n >= lit) return;
            tl.to(c.e, { attr: { fill: HF.c("acc", it.ratio == null ? 0.12 : 0.32), stroke: HF.c("acc", 0.95) }, duration: 0.2 }, at + 0.02 * c.c + 0.01 * c.r);
          });
          return parts.cap;
        }
        if (role === "range") {
          const f = it.ratio == null ? 0 : it.ratio;
          if (f > 0) {
            run.setAttribute("x2", px + pw * f);
            HF.reveal(tl, run, "draw", at, 1.1);
            tl.fromTo(pin, { attr: { cx: px, r: 9 } }, { attr: { cx: px + pw * f, r: 9 }, duration: 1.1, ease: "power2.inOut" }, at);
            return [px + pw * f, ry];
          }
          return parts.range;
        }
        if (role === "volt") {
          HF.reveal(tl, busL, "draw", at, 0.4); HF.reveal(tl, busR, "draw", at, 0.4);
          const k = it.ratio == null ? 0.6 : 0.4 + it.ratio * 0.6;
          tl.fromTo(frame, { attr: { "stroke-width": 3 } }, { attr: { "stroke-width": 3 + 5 * k }, duration: 0.25, yoyo: true, repeat: 1, ease: "power2.out" }, at + 0.3);
          return parts.volt;
        }
        return parts.other;
      },
      idle(tl, from, to) {
        HF.drive(tl, (t) => {
          const ph2 = (t % 1.8) / 1.8;
          flow.setAttribute("x", (-0.2 + ph2 * 1.2) * pw);
          flow.setAttribute("opacity", String(Math.min(1, t / 0.5)));
        }, from, Math.max(0.1, to - from));
      },
    };
  };

  /**
   * 补能:充电曲线图。横轴是时间,终点就是官方给的充电时间(真实分钟数);
   * 电量曲线(示意形状,先快后慢)从起点电量涨到终点电量 —— 起止电量从参数名里的「10%-80%」读,
   * 读不到就不标百分比。功率(kW)标在功率曲线的峰值上,峰值高度按 ratio。
   */
  SCENES.charging = function (box, items, opt) {
    const { host, sv } = shell(box, "viz-charging");
    const W = box.w, H = box.h;
    const L = W * 0.08, R = W * 0.96, T = H * 0.08, B = H * 0.82;
    const timeIt = items.find((it) => HF.role(it) === "time");
    const powIt = items.find((it) => HF.role(it) === "power");
    const win = items.map((it) => (it.label + " " + it.tag).match(/(\d+)\s*%\s*[-~至到]\s*(\d+)\s*%/)).find(Boolean);
    const s0 = win ? Number(win[1]) / 100 : 0.1, s1 = win ? Number(win[2]) / 100 : 0.8;
    const ax = HF.svg("path", line({ d: `M${L} ${T} L${L} ${B} L${R} ${B}`, stroke: HF.c("ink", 0.4), "stroke-width": 2 }), sv);
    const grid = [0.25, 0.5, 0.75].map((f) => HF.svg("line", { x1: L, y1: B - (B - T) * f, x2: R, y2: B - (B - T) * f, stroke: HF.c("ink", 0.1), "stroke-width": 1, opacity: 0 }, sv));
    const yAt = (s) => B - (B - T) * s;
    // 电量曲线:1-(1-x)^1.6 形状,示意
    const pts = [];
    for (let k = 0; k <= 40; k++) { const x = k / 40; pts.push([L + (R - L) * x, yAt(s0 + (s1 - s0) * (1 - Math.pow(1 - x, 1.6)))]); }
    const soc = HF.svg("path", line({ d: "M" + pts.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L"), stroke: HF.c("acc"), "stroke-width": 5 }), sv);
    soc.style.filter = G(opt.glow);
    const area = HF.svg("path", { d: "M" + pts.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L") + ` L${R} ${B} L${L} ${B} Z`, fill: HF.c("acc", 0.10), opacity: 0 }, sv);
    // 功率曲线:前段平台、后段回落;峰值高度按 ratio(没有就不画这条)
    let pow = null, peak = null;
    if (powIt && powIt.ratio != null) {
      const ph = yAt(0.25 + 0.7 * powIt.ratio);
      const d = `M${L} ${B} L${L + (R - L) * 0.06} ${ph} L${L + (R - L) * 0.38} ${ph} C${L + (R - L) * 0.6} ${ph} ${L + (R - L) * 0.7} ${yAt(0.3)} ${R} ${yAt(0.18)}`;
      pow = HF.svg("path", line({ d, stroke: HF.c("acc2"), "stroke-width": 3, "stroke-dasharray": "1 1" }), sv);
      peak = [L + (R - L) * 0.22, ph];
    }
    const head = HF.svg("circle", { cx: L, cy: yAt(s0), r: 0, fill: HF.c("hi") }, sv);
    const labs = [];
    if (win) { labs.push(label(sv, L - 14, yAt(s0) + 8, win[1] + "%", { anchor: "end" })); labs.push(label(sv, L - 14, yAt(s1) + 8, win[2] + "%", { anchor: "end" })); }
    labs.push(label(sv, L, B + 40, "0", {}));
    const tEnd = timeIt ? label(sv, R, B + 40, timeIt.value, { anchor: "end", fill: HF.c("acc"), size: 26 }) : null;
    const parts = { time: [R - 20, yAt(s1)], power: peak || [L + (R - L) * 0.3, yAt(0.6)], other: [(L + R) / 2, (T + B) / 2] };
    let drawn = false;
    return {
      host, parts,
      show(tl, at) {
        HF.reveal(tl, ax, "draw", at, 0.8);
        tl.to(grid, { opacity: 1, duration: 0.4, stagger: 0.06 }, at + 0.4);
        tl.to(labs, { opacity: 1, duration: 0.4 }, at + 0.6);
      },
      hit(tl, it, at) {
        const role = HF.role(it);
        if ((role === "time" || role === "other") && !drawn) {
          drawn = true;
          HF.reveal(tl, soc, "draw", at, 1.6);
          tl.to(area, { opacity: 1, duration: 1.2 }, at + 0.4);
          const o = { f: 0 };
          tl.to(o, { f: 1, duration: 1.6, ease: "power2.inOut", onUpdate: () => {
            const n = Math.round(o.f * 40); head.setAttribute("cx", pts[n][0]); head.setAttribute("cy", pts[n][1]); } }, at);
          tl.to(head, { attr: { r: 9 }, duration: 0.2 }, at);
          if (tEnd) tl.to(tEnd, { opacity: 1, duration: 0.4 }, at + 1.4);
          return parts.time;
        }
        if (role === "power" && pow) { HF.reveal(tl, pow, "draw", at, 1.2); return parts.power; }
        return parts.other;
      },
      idle(tl, from, to) {
        HF.drive(tl, (t) => { head.setAttribute("r", String(8 + 3 * Math.sin(t * 5))); }, from, Math.max(0.1, to - from));
      },
    };
  };

  /**
   * 加速:0–100 km/h 速度弧 + 秒表。指针扫过 0→100 用的就是官方零百秒数(真实时间,≤9 秒才按真实走,
   * 长于这一拍剩余时间时压缩并在秒表上写原文)。下方一条"同级位置"刻度:10 秒到 2 秒,标出这台车在哪(按 ratio)。
   */
  SCENES.accel = function (box, items, opt) {
    const { host, sv } = shell(box, "viz-accel");
    const W = box.w, H = box.h;
    const cx = W * 0.5, cy = H * 0.62, r = Math.min(W * 0.36, H * 0.52);
    const a0 = Math.PI * 0.85, a1 = Math.PI * 2.15;
    const pt = (a, rr) => [cx + Math.cos(a) * rr, cy + Math.sin(a) * rr];
    const arc = (rr, f) => { const e = a0 + (a1 - a0) * f; const p0 = pt(a0, rr), p1 = pt(e, rr); return `M${p0[0]} ${p0[1]} A${rr} ${rr} 0 ${e - a0 > Math.PI ? 1 : 0} 1 ${p1[0]} ${p1[1]}`; };
    const track = HF.svg("path", line({ d: arc(r, 1), stroke: HF.c("ink", 0.18), "stroke-width": 14 }), sv);
    const fill = HF.svg("path", line({ d: arc(r, 1), stroke: HF.c("acc"), "stroke-width": 14 }), sv);
    fill.style.filter = G(opt.glow);
    const ticks = [];
    for (let k = 0; k <= 10; k++) {
      const a = a0 + (a1 - a0) * k / 10, p0 = pt(a, r - 30), p1 = pt(a, r - (k % 5 ? 44 : 60));
      ticks.push(HF.svg("line", { x1: p0[0], y1: p0[1], x2: p1[0], y2: p1[1], stroke: HF.c("ink", 0.5), "stroke-width": k % 5 ? 2 : 3, opacity: 0 }, sv));
      if (k % 5 === 0) { const q = pt(a, r - 92); ticks.push(label(sv, q[0], q[1] + 8, String(k * 10), { anchor: "middle", size: 24 })); }
    }
    const needle = HF.svg("line", { x1: cx, y1: cy, x2: cx, y2: cy, stroke: HF.c("hi"), "stroke-width": 4, opacity: 0 }, sv);
    const aim = (f, j) => { const q = pt(a0 + (a1 - a0) * f, r - 70);
      needle.setAttribute("x2", q[0].toFixed(1)); needle.setAttribute("y2", (q[1] + (j || 0)).toFixed(1)); };
    aim(0);
    let speed = 0;
    const hub = HF.svg("circle", { cx, cy, r: 12, fill: HF.c("hi"), opacity: 0 }, sv);
    const kmh = label(sv, cx, cy + r * 0.42, "0 km/h", { anchor: "middle", size: 30, fill: HF.c("ink", 0.85) });
    // 同级位置刻度
    const zero = items.find((it) => HF.role(it) === "zero");
    const sy = H * 0.97, sx0 = W * 0.18, sx1 = W * 0.82;
    const scale = HF.svg("line", line({ x1: sx0, y1: sy, x2: sx1, y2: sy, stroke: HF.c("ink", 0.3), "stroke-width": 2 }), sv);
    const sl = [label(sv, sx0, sy + 34, "10s", { anchor: "middle", size: 20 }), label(sv, sx1, sy + 34, "2s", { anchor: "middle", size: 20 })];
    const mark = zero && zero.ratio != null ? HF.svg("path", { d: `M${sx0 + (sx1 - sx0) * zero.ratio} ${sy - 4} l-10 -16 l20 0 Z`, fill: HF.c("acc"), opacity: 0 }, sv) : null;
    const parts = { zero: [cx, cy - r * 0.2], top: [cx + r * 0.6, cy - r * 0.6], power: [cx - r * 0.6, cy - r * 0.5], other: [cx, cy] };
    return {
      host, parts,
      show(tl, at) {
        HF.reveal(tl, track, "draw", at, 0.9);
        tl.to(ticks, { opacity: 1, duration: 0.25, stagger: 0.03 }, at + 0.3);
        tl.to([needle, hub, kmh], { opacity: 1, duration: 0.3 }, at + 0.6);
        if (mark) { HF.reveal(tl, scale, "draw", at + 0.5, 0.6); tl.to(sl, { opacity: 1, duration: 0.3 }, at + 0.8); }
      },
      hit(tl, it, at) {
        const role = HF.role(it);
        if (role === "zero" && it.num) {
          const room = Math.max(1.0, HF.seconds - at - 1.2);
          const run = it.num <= 9 ? Math.min(it.num, room) : Math.min(2.5, room);
          tl.fromTo(fill, { attr: { "stroke-dashoffset": 1 } }, { attr: { "stroke-dashoffset": 0 }, duration: run, ease: "power1.out" }, at);
          const ez = gsap.parseEase("power1.out");
          HF.drive(tl, (t) => { speed = 100 * ez(Math.min(1, t / run)); kmh.textContent = Math.round(speed) + " km/h"; aim(speed / 100); }, at, run + 0.01);
          if (mark) tl.fromTo(mark, { opacity: 0, y: -20 }, { opacity: 1, y: 0, duration: 0.4, ease: "back.out(2)" }, at + run);
          return parts.zero;
        }
        return parts[role] || parts.other;
      },
      idle(tl, from, to) {
        // 到 100 之后指针在终点轻轻抖
        HF.drive(tl, (t) => { if (speed >= 99.5) aim(1, Math.sin(t * 40) * 1.2); }, from, Math.max(0.1, to - from));
      },
    };
  };

  /**
   * 尺寸:侧影(有车图用车图,没有用示意线稿)+ 尺寸线。车长、车高、轴距的尺寸线画在真实位置上;
   * 尺寸线本身就是测量,不需要量程。车宽在侧视里看不到,单独标在右上角。
   */
  SCENES.dims = function (box, items, opt) {
    const { host, sv } = shell(box, "viz-dims");
    const W = box.w, H = box.h;
    const P = D.photo;
    const geo = P || { width: 100, height: 34, floor: 0.95, roof: 0.05, wheels: [0.239, 0.771], body: [0.02, 0.98], facing: "right" };
    const cw = W * 0.78, ch = cw * geo.height / geo.width;
    const cx0 = (W - cw) / 2, cy0 = H * 0.5 - ch * 0.55;
    let car;
    if (P) {
      car = HF.el("img", "viz-car"); car.src = P.src;
      Object.assign(car.style, { position: "absolute", left: cx0 + "px", top: cy0 + "px", width: cw + "px", height: ch + "px", opacity: 0 });
      host.insertBefore(car, sv);
    } else {
      car = HF.svg("rect", line({ x: cx0 + cw * 0.02, y: cy0 + ch * 0.2, width: cw * 0.96, height: ch * 0.72, rx: ch * 0.2, stroke: HF.c("ink", 0.7), "stroke-width": 3 }), sv);
    }
    const X = (u) => cx0 + u * cw, Y = (v) => cy0 + v * ch;
    const floor = Y(geo.floor), roof = Y(geo.roof);
    const b0 = X(geo.body[0]), b1 = X(geo.body[1]), w0 = X(geo.wheels[0]), w1 = X(geo.wheels[1]);
    const dim = (x1, y1, x2, y2, horiz) => {
      const g = HF.svg("g", {}, sv);
      const l = HF.svg("line", line({ x1, y1, x2, y2, stroke: HF.c("acc"), "stroke-width": 2.5 }), g);
      const e = horiz ? [[x1, y1 - 14, x1, y1 + 14], [x2, y2 - 14, x2, y2 + 14]] : [[x1 - 14, y1, x1 + 14, y1], [x2 - 14, y2, x2 + 14, y2]];
      const caps = e.map((c) => HF.svg("line", { x1: c[0], y1: c[1], x2: c[2], y2: c[3], stroke: HF.c("acc"), "stroke-width": 2.5, opacity: 0 }, g));
      return { l, caps };
    };
    const D_ = {
      len: dim(b0, floor + 70, b1, floor + 70, true),
      wb: dim(w0, floor + 30, w1, floor + 30, true),
      hgt: dim(b1 + 50, roof, b1 + 50, floor, false),
    };
    const parts = { len: [(b0 + b1) / 2, floor + 70], wb: [(w0 + w1) / 2, floor + 30], hgt: [b1 + 50, (roof + floor) / 2],
                    wid: [W * 0.85, roof - 20], other: [(b0 + b1) / 2, (roof + floor) / 2] };
    const ground = HF.svg("line", line({ x1: W * 0.04, y1: floor, x2: W * 0.96, y2: floor, stroke: HF.c("ink", 0.25), "stroke-width": 2 }), sv);
    return {
      host, parts,
      show(tl, at) {
        HF.reveal(tl, ground, "draw", at, 0.8);
        if (P) tl.fromTo(car, { opacity: 0, x: -60 }, { opacity: 1, x: 0, duration: 1.0, ease: "expo.out" }, at + 0.1);
        else HF.reveal(tl, car, "draw", at + 0.1, 1.0);
      },
      hit(tl, it, at) {
        const role = HF.role(it);
        const d = D_[role];
        if (d) { HF.reveal(tl, d.l, "draw", at, 0.7); tl.to(d.caps, { opacity: 1, duration: 0.2 }, at); }
        return parts[role] || parts.other;
      },
      idle() {},
    };
  };

  /**
   * 感知:侧视,车(有车图用车图)+ 从车顶前沿射出的扫描扇面。射程圈按真实米数画,
   * 最远一圈就是官方最远探测距离;没有距离参数就只画扫描,不画射程。
   */
  SCENES.lidar = function (box, items, opt) {
    const { host, sv } = shell(box, "viz-lidar");
    const W = box.w, H = box.h;
    const P = D.photo;
    const geo = P || { width: 100, height: 34, floor: 0.95, roof: 0.05, wheels: [0.239, 0.771], body: [0.02, 0.98], facing: "right", profile: [] };
    const fr = geo.facing === "left" ? -1 : 1;
    // 车放在扫描方向的反侧,前方留出射程
    const cw = W * 0.56, ch = cw * geo.height / geo.width;
    const cx0 = fr > 0 ? W * 0.02 : W * 0.56, cy0 = H * 0.78 - geo.floor * ch;
    let car;
    if (P) {
      car = HF.el("img", "viz-car"); car.src = P.src;
      Object.assign(car.style, { position: "absolute", left: cx0 + "px", top: cy0 + "px", width: cw + "px", height: ch + "px", opacity: 0 });
      host.insertBefore(car, sv);
    } else {
      car = HF.svg("rect", line({ x: cx0 + cw * 0.02, y: cy0 + ch * 0.2, width: cw * 0.96, height: ch * 0.72, rx: ch * 0.2, stroke: HF.c("ink", 0.7), "stroke-width": 3 }), sv);
    }
    // 雷达在车顶前沿:从车头往回找,第一处接近车顶最高点的位置
    const pr = geo.profile || [];
    let u = fr > 0 ? 0.62 : 0.38, v = geo.roof + 0.02;
    if (pr.length) {
      const top = Math.min(...pr), idx = [...pr.keys()];
      if (fr > 0) idx.reverse();
      const k = idx.find((i) => pr[i] <= top + 0.035);
      u = geo.body[0] + (geo.body[1] - geo.body[0]) * k / (pr.length - 1); v = pr[k];
    }
    const ox = cx0 + u * cw, oy = cy0 + v * ch;
    const floorY = cy0 + geo.floor * ch;
    const ground = HF.svg("line", line({ x1: 0, y1: floorY, x2: W, y2: floorY, stroke: HF.c("ink", 0.25), "stroke-width": 2 }), sv);
    const R = (fr > 0 ? W - ox : ox) * 0.98, fanA = 0.2;
    const P2 = (a, rr) => [ox + fr * Math.cos(a) * rr, oy + Math.sin(a) * rr];
    const sweep = fr > 0 ? 1 : 0;
    const arcD = (rr) => { const p0 = P2(-fanA, rr), p1 = P2(fanA, rr); return `M${p0[0]} ${p0[1]} A${rr} ${rr} 0 0 ${sweep} ${p1[0]} ${p1[1]}`; };
    const e0 = P2(-fanA, R), e1 = P2(fanA, R);
    const fan = HF.svg("path", { d: `M${ox} ${oy} L${e0[0]} ${e0[1]} A${R} ${R} 0 0 ${sweep} ${e1[0]} ${e1[1]} Z`,
      fill: HF.c("acc", 0.08), stroke: HF.c("acc", 0.4), "stroke-width": 1.5, opacity: 0 }, sv);
    const beam = HF.svg("line", { x1: ox, y1: oy, x2: ox + fr * R, y2: oy, stroke: HF.c("hi", 0.9), "stroke-width": 3, opacity: 0 }, sv);
    beam.style.filter = G(opt.glow);
    const src = HF.svg("circle", { cx: ox, cy: oy, r: 7, fill: HF.c("hi"), opacity: 0 }, sv);
    // 射程圈:50 / 100 米一档,最远一圈 = 最远探测距离
    const dist = items.find((it) => HF.role(it) === "dist" && it.num);
    const rings = [];
    if (dist) {
      const maxM = dist.num, step = maxM <= 200 ? 50 : 100;
      const marks = [];
      for (let m = step; m < maxM - 1e-6; m += step) marks.push(m);
      marks.push(maxM);
      marks.forEach((m) => {
        const last = m === maxM, rr = R * m / maxM;
        const a = HF.svg("path", line({ d: arcD(rr), stroke: last ? HF.c("acc", 0.9) : HF.c("ink", 0.3), "stroke-width": last ? 3 : 1.5 }), sv);
        const q = P2(-fanA, rr);
        rings.push({ a, t: label(sv, q[0], q[1] - 14, m + "m", { anchor: "middle", size: last ? 26 : 20, fill: last ? HF.c("acc") : HF.c("ink", 0.6) }) });
      });
    }
    // 目标点:落在扇面里,固定种子
    const rnd = HF.rng(9), dots = [];
    for (let k = 0; k < 24; k++) {
      const rr = R * (0.2 + rnd() * 0.78), a = (rnd() - 0.5) * 2 * fanA * 0.9, q = P2(a, rr);
      dots.push({ e: HF.svg("circle", { cx: q[0], cy: q[1], r: 3 + rnd() * 3, fill: HF.c("acc"), opacity: 0 }, sv), a });
    }
    const parts = { dist: P2(0, R * 0.9), count: [ox, oy], other: P2(0, R * 0.4) };
    return {
      host, parts,
      show(tl, at) {
        HF.reveal(tl, ground, "draw", at, 0.8);
        if (P) tl.fromTo(car, { opacity: 0, x: -40 * fr }, { opacity: 1, x: 0, duration: 1.0, ease: "expo.out" }, at + 0.1);
        else HF.reveal(tl, car, "draw", at + 0.1, 1.0);
        tl.to(src, { opacity: 1, duration: 0.3 }, at + 0.6);
        tl.to(fan, { opacity: 1, duration: 0.6 }, at + 0.7);
        tl.to(beam, { opacity: 1, duration: 0.3 }, at + 0.8);
      },
      hit(tl, it, at) {
        const role = HF.role(it);
        if (role === "dist") {
          rings.forEach((r, k) => { HF.reveal(tl, r.a, "draw", at + k * 0.15, 0.6); tl.to(r.t, { opacity: 1, duration: 0.3 }, at + k * 0.15 + 0.3); });
          return parts.dist;
        }
        if (role === "count") tl.fromTo(src, { attr: { r: 7 } }, { attr: { r: 16 }, duration: 0.3, yoyo: true, repeat: 1 }, at);
        return parts[role] || parts.other;
      },
      idle(tl, from, to) {
        HF.drive(tl, (t) => {
          const a = Math.sin(t * 1.8) * fanA * 0.95, q = P2(a, R);
          beam.setAttribute("x2", q[0].toFixed(1)); beam.setAttribute("y2", q[1].toFixed(1));
          dots.forEach((d) => { const near = Math.max(0, 1 - Math.abs(d.a - a) / 0.06); d.e.setAttribute("opacity", (0.12 + 0.88 * near).toFixed(2)); });
        }, from, Math.max(0.1, to - from));
      },
    };
  };

  /**
   * 芯片:封装 + die 上的计算单元阵列,数据流沿引脚进出。算力(TOPS)按 ratio 点亮相应比例的单元;
   * 没有 ratio 就整体通电一遍。芯片名写在 die 上。
   */
  SCENES.chip = function (box, items, opt) {
    const { host, sv } = shell(box, "viz-chip");
    const W = box.w, H = box.h;
    const S = Math.min(W, H) * 0.62, x0 = (W - S) / 2, y0 = (H - S) / 2;
    const pkg = HF.svg("rect", line({ x: x0, y: y0, width: S, height: S, rx: 24, stroke: HF.c("ink", 0.7), "stroke-width": 3 }), sv);
    const die = HF.svg("rect", line({ x: x0 + S * 0.16, y: y0 + S * 0.16, width: S * 0.68, height: S * 0.68, rx: 10, stroke: HF.c("acc"), "stroke-width": 3 }), sv);
    die.style.filter = G(opt.glow);
    const N = 8, cells = [];
    const cs = S * 0.68 / N;
    for (let r = 0; r < N; r++) for (let c = 0; c < N; c++)
      cells.push(HF.svg("rect", { x: x0 + S * 0.16 + c * cs + 3, y: y0 + S * 0.16 + r * cs + 3, width: cs - 6, height: cs - 6, rx: 3,
        fill: HF.c("acc", 0), stroke: HF.c("acc", 0.25), "stroke-width": 1, opacity: 0 }, sv));
    const pins = [];
    for (let k = 0; k < 10; k++) {
      const f = 0.1 + k * 0.09;
      [[x0 + S * f, y0, x0 + S * f, y0 - 50], [x0 + S * f, y0 + S, x0 + S * f, y0 + S + 50],
       [x0, y0 + S * f, x0 - 50, y0 + S * f], [x0 + S, y0 + S * f, x0 + S + 50, y0 + S * f]].forEach((p) =>
        pins.push(HF.svg("line", line({ x1: p[0], y1: p[1], x2: p[2], y2: p[3], stroke: HF.c("ink", 0.4), "stroke-width": 2 }), sv)));
    }
    const nameIt = items.find((it) => HF.role(it) === "chipname");
    const name = nameIt ? label(sv, W / 2, y0 + S * 0.94, nameIt.value, { anchor: "middle", size: 22, fill: HF.c("ink", 0.8) }) : null;
    const pulses = pins.filter((_, k) => k % 3 === 0).map((p) => ({ p, c: HF.svg("circle", { r: 4, fill: HF.c("hi"), opacity: 0 }, sv) }));
    const parts = { tops: [W / 2, H / 2], chipname: [W / 2, y0 + S * 0.9], other: [W / 2, H / 2] };
    return {
      host, parts,
      show(tl, at) {
        HF.reveal(tl, pkg, "draw", at, 0.8);
        HF.reveal(tl, die, "draw", at + 0.3, 0.8);
        tl.to(cells, { opacity: 1, duration: 0.2, stagger: { each: 0.008, from: "center" } }, at + 0.5);
        pins.forEach((p, k) => HF.reveal(tl, p, "draw", at + 0.4 + (k % 10) * 0.03, 0.4));
        if (name) tl.to(name, { opacity: 1, duration: 0.4 }, at + 1.0);
      },
      hit(tl, it, at) {
        const role = HF.role(it);
        if (role === "tops") {
          const lit = it.ratio == null ? cells.length : Math.max(1, Math.round(it.ratio * cells.length));
          cells.forEach((c, n) => { if (n < lit) tl.to(c, { attr: { fill: HF.c("acc", it.ratio == null ? 0.15 : 0.45), stroke: HF.c("acc", 0.9) }, duration: 0.15 }, at + n * 0.012); });
          return parts.tops;
        }
        return parts[role] || parts.other;
      },
      idle(tl, from, to) {
        HF.drive(tl, (t) => {
          pulses.forEach((q, k) => {
            const f = ((t * 0.9 + k * 0.17) % 1);
            const x1 = +q.p.getAttribute("x1"), y1 = +q.p.getAttribute("y1"), x2 = +q.p.getAttribute("x2"), y2 = +q.p.getAttribute("y2");
            q.c.setAttribute("cx", x2 + (x1 - x2) * f); q.c.setAttribute("cy", y2 + (y1 - y2) * f);
            q.c.setAttribute("opacity", String(Math.min(1, (t - 0) * 2) * Math.sin(f * Math.PI)));
          });
        }, from, Math.max(0.1, to - from));
      },
    };
  };

  /** 对比:每行两条横条,长短按两个值的比例(share,同单位才有);没有比例只并排写数字,不画条 */
  SCENES.compare = function (box, items, opt) {
    const { host, sv } = shell(box, "viz-compare");
    const W = box.w, H = box.h;
    const n = Math.max(1, items.length), rowH = Math.min(H / n, 220);
    const x0 = W * 0.04, bw = W * 0.74;
    const rows = items.map((it, k) => {
      const y = (H - rowH * n) / 2 + k * rowH + rowH * 0.3;
      const name = label(sv, x0, y - 14, it.label, { size: 24, fill: HF.c("ink", 0.7) });
      const vals = [it.value, it.b];
      // 没有比例(单位不同 / 不是数字):两个值并排写,不画条
      const share = it.share || null;
      const bars = [], texts = [];
      vals.forEach((v, j) => {
        if (!v) return;
        const by = y + j * rowH * 0.3, bh = rowH * 0.22;
        const len = share ? Math.max(4, bw * share[j]) : 0;
        if (share) {
          const b = HF.svg("rect", { x: x0, y: by, width: len, height: bh, rx: rowH * 0.06, fill: j ? HF.c("acc2", 0.85) : HF.c("acc"), opacity: 0 }, sv);
          b.style.transformOrigin = `${x0}px 0px`; b.style.transformBox = "view-box";
          bars.push(b);
        }
        texts.push(label(sv, x0 + len + (share ? 16 : j * 260), by + bh * 0.78, v,
          { size: Math.round(bh * 0.8), weight: 600, fill: j ? HF.c("acc2") : HF.c("ink", 0.95) }));
      });
      return { y, bars, texts, name };
    });
    // 图例:两列各是谁
    const leg = (HF.cols.length >= 2 && rows.some((r) => r.bars.length)) ? HF.cols.slice(0, 2).map((name, j) => {
      const g = HF.svg("g", { opacity: 0 }, sv);
      HF.svg("rect", { x: x0 + j * 260, y: -10, width: 26, height: 14, rx: 4, fill: j ? HF.c("acc2", 0.85) : HF.c("acc") }, g);
      label(g, x0 + j * 260 + 40, 4, name, { opacity: 1, size: 24, fill: HF.c("ink", 0.85) });
      return g;
    }) : [];
    return {
      host, parts: { other: [W / 2, H / 2] },
      show(tl, at) { if (leg.length) tl.to(leg, { opacity: 1, duration: 0.5, stagger: 0.1 }, at); },
      hit(tl, it, at) {
        const k = items.indexOf(it);
        const r = rows[k];
        if (r) {
          tl.to(r.name, { opacity: 1, duration: 0.3 }, at);
          r.bars.forEach((b, j) => tl.fromTo(b, { opacity: 1, scaleX: 0 }, { opacity: 1, scaleX: 1, duration: 0.9, ease: "expo.out" }, at + j * 0.15));
          r.texts.forEach((t, j) => tl.fromTo(t, { opacity: 0, x: -12 }, { opacity: 1, x: 0, duration: 0.4 }, at + 0.6 + j * 0.15));
        }
        return r ? [x0 + bw * 0.5, r.y + rowH * 0.2] : [W / 2, H / 2];
      },
      idle() {},
    };
  };

  HF.viz = {
    /**
     * 按场景建画面组件。box = {x, y, w, h} 页面像素;items = HF.items();opt.glow 辉光强度,opt.font 图里标注的字体。
     * 返回 { host, parts, show(tl, at), hit(tl, item, at) → [x, y] 这一行对应部位(box 内坐标),
     *        idle(tl, from, to) 停留段的持续动作 }。不认识的场景返回 null(模版只做大字)。
     */
    make(scene, box, items, opt) {
      const f = SCENES[scene];
      FONT = (opt && opt.font) || "Montserrat, Noto Sans SC, sans-serif";
      return f ? f(box, items, Object.assign({ glow: 1 }, opt || {})) : null;
    },
    scenes: Object.keys(SCENES),
  };
})();
