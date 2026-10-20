/* 小样共用的几个小工具。全部按时间轴驱动,可任意顺序 seek。 */
(function () {
  var HF = {};
  // 数字滚动:from → to,按 to 原文的小数位显示;结束时写回原文,保证停在核对过的字面
  HF.countUp = function (tl, el, text, at, dur, opt) {
    opt = opt || {};
    var m = String(text).match(/-?\d+(?:\.\d+)?/);
    if (!m) { tl.set(el, { textContent: text }, at); return; }
    var to = parseFloat(m[0]), dec = (m[0].split(".")[1] || "").length;
    var from = opt.from == null ? 0 : opt.from;
    var st = { v: from };
    el.textContent = from.toFixed(dec);
    tl.to(st, {
      v: to, duration: dur, ease: opt.ease || "power3.out",
      onUpdate: function () { el.textContent = st.v.toFixed(dec); },
      onComplete: function () { el.textContent = m[0]; },
    }, at);
  };
  // 每帧回调:用属性 setter 驱动,seek 时也一定会触发(onUpdate 在静默 seek 下可能不触发)
  HF.drive = function (tl, fn, at, dur) {
    var d = { _t: 0 };
    Object.defineProperty(d, "t", { get: function () { return this._t; }, set: function (v) { this._t = v; fn(v); } });
    tl.fromTo(d, { t: 0 }, { t: dur, duration: dur, ease: "none" }, at);
  };
  window.HF = HF;
})();
