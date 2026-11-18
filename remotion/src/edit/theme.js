// 剪辑 v2 共用的字体、描边、字宽估算、数字拆分等小工具(classic 与 commercial 两套包装都用)。
import { Easing, interpolate, spring, staticFile } from "remotion";

// EditFont = EDIT_V2_FONT_FILE 指定的字体(合成时嵌入);没配就按顺序找系统里的粗黑体
export const FONT = '"EditFont","Noto Sans SC Black","Source Han Sans SC Heavy","Source Han Sans CN Heavy",'
  + '"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","WenQuanYi Zen Hei",sans-serif';
export const YELLOW = "#FFE14D";

export const TRANS = 0.28; // classic 转场时长(秒)
export const ease = Easing.bezier(0.16, 1, 0.3, 1);
export const easeInOut = Easing.bezier(0.45, 0, 0.55, 1);
export const clamp01 = (x) => Math.max(0, Math.min(1, x));

export const src = (s) => (/^(https?:|data:)/.test(s) ? s : staticFile(s));

// 白字黑描边 + 投影:压在任何画面上都清楚
export const strokeText = (size, w = 0.1, color = "#000") => ({
  WebkitTextStroke: `${Math.max(3, Math.round(size * w))}px ${color}`, paintOrder: "stroke fill",
  textShadow: `0 ${Math.round(size * 0.06)}px ${Math.round(size * 0.12)}px rgba(0,0,0,.55)`,
});

// 估算一行字的宽度(em):汉字 1,英文数字约 0.58
export const textEm = (text) => [...String(text || "")].reduce(
  (n, c) => n + (/[\u0000-ɏ]/.test(c) ? 0.58 : 1), 0);
// 字号上限:这一行在 maxW 像素内放得下(长关键词 / 长标题不出屏)
export const fitSize = (text, size, maxW, extraEm = 0) =>
  Math.max(12, Math.min(size, maxW / Math.max(1, textEm(text) + extraEm)));

// 色块底花字
export const plate = (bg, color, size) => ({
  background: bg, color, padding: "0.06em 0.32em",
  borderRadius: Math.round(size * 0.1), boxDecorationBreak: "clone", WebkitBoxDecorationBreak: "clone",
  boxShadow: `0 ${Math.round(size * 0.08)}px ${Math.round(size * 0.2)}px rgba(0,0,0,.35)`,
});

// 数字 / 英文型号上强调色(只给已经挑好的字上色,不用来挑句子)
export const HOT = /(\d[\d,.]*\s*(?:万元?|公里|km|kWh|秒|马力|匹|英寸|mm|毫米|米|%|倍)?|[A-Za-z][A-Za-z0-9-]*\d[A-Za-z0-9-]*|[A-Z]{2,})/g;
export const hotSpans = (text) => {
  const out = [];
  let pos = 0;
  const t = String(text);
  for (const m of t.matchAll(HOT)) {
    if (m.index > pos) out.push({ t: t.slice(pos, m.index), hot: false });
    out.push({ t: m[0], hot: true });
    pos = m.index + m[0].length;
  }
  if (pos < t.length) out.push({ t: t.slice(pos), hot: false });
  return out;
};

// 标题断成至多两行:一行放得下就不拆;要拆时优先在标点后断,其次离正中最近、
// 且不切开数字 / 型号(HOT 片段)的位置
export const splitTitle = (title, perLine) => {
  const t = String(title || "").trim();
  if (t.length <= perLine) return [t];
  const banned = new Set();
  for (const m of t.matchAll(HOT)) for (let k = m.index + 1; k < m.index + m[0].length; k++) banned.add(k);
  // 英文 / 数字 / 连字符连着的("Model Y"、"DM-i"、"3.23")中间都不断
  const w = (c) => /[A-Za-z0-9.\-]/.test(c || "");
  const inWord = (i) => (w(t[i - 1]) && w(t[i]))
    || (t[i - 1] === " " && w(t[i - 2]) && w(t[i])) || (t[i] === " " && w(t[i - 1]) && w(t[i + 1]));
  const mid = t.length / 2;
  const ok = (i) => !banned.has(i) && !inWord(i) && i > 0 && i < t.length;
  let best = -1;
  for (let i = 1; i < t.length; i++) {
    if (ok(i) && "，,：:？?！!、".includes(t[i - 1]) && Math.abs(i - mid) <= t.length * 0.3
        && (best < 0 || Math.abs(i - mid) < Math.abs(best - mid))) best = i;
  }
  if (best < 0) {
    for (let d = 0; d < t.length; d++) {
      for (const i of [Math.round(mid) - d, Math.round(mid) + d]) if (best < 0 && ok(i)) best = i;
      if (best >= 0) break;
    }
  }
  return [t.slice(0, best).trim(), t.slice(best).trim()];
};

// "620km" → [620, 0 位小数, "", "km"];"1,210匹" → [1210, 0, "", "匹"]
export const splitNumber = (value) => {
  const m = String(value).match(/^([^\d-]*)(-?[\d,]+(?:\.\d+)?)(.*)$/);
  if (!m) return null;
  const num = parseFloat(m[2].replace(/,/g, ""));
  const dec = (m[2].split(".")[1] || "").length;
  return [num, dec, m[1], m[3]];
};

// 数字从 0 滚到目标值(p:0~1);拆不出数字的原样返回
export const countUp = (value, p) => {
  const parts = splitNumber(value);
  if (!parts) return String(value);
  return `${parts[2]}${(parts[0] * clamp01(p)).toFixed(parts[1])}${parts[3]}`;
};

export const popIn = (frame, fps, delay = 0, cfg = { damping: 11, stiffness: 200 }) =>
  spring({ frame: frame - Math.round(delay * fps), fps, config: cfg });

// 进 / 出场:前 inS 秒从 0 到 1,最后 outS 秒回到 0(缓动)
export const inOut = (t, dur, inS = 0.3, outS = 0.25) => {
  const a = ease(clamp01(t / inS));
  const b = outS > 0 ? ease(clamp01((dur - t) / outS)) : 1;
  return Math.min(a, b);
};

export const lerp = (p, a, b) => interpolate(p, [0, 1], [a, b]);
