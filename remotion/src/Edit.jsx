// 剪辑 v2 主合成:读 app/pipeline/edit/edl.py 产出的时间线,一次合成整片(静音视频,混音另走 ffmpeg)。
//
// 竖屏版面(对标抖音信息类):
//   顶部   标题带(片名) + 章节进度条
//   中部   素材带:实拍按子镜头推拉、转场;叠加层(钩子大字 / 关键词 / 大数字 / 参数面板 / 章节标牌)压在这里
//   下部   花字字幕(关键词变色弹出)
//   整屏卡片 / 概念动画 / 片尾卡(kind=beat)直接铺满,不画标题带和素材带
// 横屏:素材带即全屏。
import React, { useEffect, useState } from "react";
import {
  AbsoluteFill, Img, OffthreadVideo, Sequence, interpolate, spring, staticFile,
  useCurrentFrame, useVideoConfig, Easing, continueRender, delayRender,
} from "remotion";
import { Cue } from "./Layer.jsx";

// EditFont = EDIT_V2_FONT_FILE 指定的字体(合成时嵌入);没配就按顺序找系统里的粗黑体
const FONT = '"EditFont","Noto Sans SC Black","Source Han Sans SC Heavy","Source Han Sans CN Heavy",'
  + '"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","WenQuanYi Zen Hei",sans-serif';
const YELLOW = "#FFE14D";
// 白字黑描边 + 投影:压在任何画面上都清楚
const strokeText = (size, w = 0.1) => ({
  WebkitTextStroke: `${Math.max(3, Math.round(size * w))}px #000`, paintOrder: "stroke fill",
  textShadow: `0 ${Math.round(size * 0.06)}px ${Math.round(size * 0.12)}px rgba(0,0,0,.55)`,
});
// 估算一行字的宽度(em):汉字 1,英文数字约 0.58;花字色块左右各 0.32em
export const textEm = (text) => [...String(text || "")].reduce(
  (n, c) => n + (/[\u0000-\u024f]/.test(c) ? 0.58 : 1), 0);
// 字号上限:这一行在 maxW 像素内放得下(长关键词 / 长标题不出屏)
export const fitSize = (text, size, maxW, extraEm = 0) =>
  Math.max(12, Math.min(size, maxW / Math.max(1, textEm(text) + extraEm)));
// 色块底花字
const plate = (bg, color, size) => ({
  background: bg, color, padding: "0.06em 0.32em",
  borderRadius: Math.round(size * 0.1), boxDecorationBreak: "clone", WebkitBoxDecorationBreak: "clone",
  boxShadow: `0 ${Math.round(size * 0.08)}px ${Math.round(size * 0.2)}px rgba(0,0,0,.35)`,
});
// 数字 / 英文型号上强调色
const HOT = /(\d[\d,.]*\s*(?:万元?|公里|km|kWh|秒|马力|匹|英寸|mm|毫米|米|%|倍)?|[A-Za-z][A-Za-z0-9-]*\d[A-Za-z0-9-]*|[A-Z]{2,})/g;
export const hotSpans = (text) => {
  const out = [];
  let pos = 0;
  for (const m of String(text).matchAll(HOT)) {
    if (m.index > pos) out.push({ t: text.slice(pos, m.index), hot: false });
    out.push({ t: m[0], hot: true });
    pos = m.index + m[0].length;
  }
  if (pos < text.length) out.push({ t: text.slice(pos), hot: false });
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

const TRANS = 0.28; // 转场时长(秒)

const ease = Easing.bezier(0.16, 1, 0.3, 1);

const src = (s) => (/^(https?:|data:)/.test(s) ? s : staticFile(s));

// 竖屏版面网格(相对高度)。底部 ~20% 和右侧一列会被抖音的文案 / 按钮盖住,字和数据都不放那里
const grid = (w, h) => {
  if (w >= h) return { band: { top: 0, height: h }, title: null, capBottom: 0.1 };
  return {
    title: { top: h * 0.05, height: h * 0.17 },
    band: { top: h * 0.235, height: h * 0.46 },
    capBottom: 0.2,
  };
};

// 嵌入字体:FontFace 加载完才放行出帧,避免前几帧用了回退字体
const useEmbeddedFont = (file) => {
  const [handle] = useState(() => (file ? delayRender("edit-font") : null));
  useEffect(() => {
    if (!file) return;
    const face = new FontFace("EditFont", `url(${staticFile(file)})`);
    face.load().then((f) => { document.fonts.add(f); continueRender(handle); })
      .catch(() => continueRender(handle));
  }, [file, handle]);
};

// ---------------------------------------------------------------- 素材带里的一刀
const motionTransform = (motion, p) => {
  switch (motion) {
    case "push": return `scale(${interpolate(p, [0, 1], [1.0, 1.12])})`;
    case "pull": return `scale(${interpolate(p, [0, 1], [1.12, 1.0])})`;
    case "pan_l": return `scale(1.16) translateX(${interpolate(p, [0, 1], [3.5, -3.5])}%)`;
    case "pan_r": return `scale(1.16) translateX(${interpolate(p, [0, 1], [-3.5, 3.5])}%)`;
    default: return "none";
  }
};

const transitionStyle = (kind, t) => {
  // t:进入这一刀后的秒数
  if (t >= TRANS || kind === "cut" || kind === "flash") return {};
  const p = ease(Math.min(1, t / TRANS));
  if (kind === "whip") {
    return { transform: `translateX(${(1 - p) * 60}%)`, filter: `blur(${(1 - p) * 18}px)` };
  }
  if (kind === "zoom") {
    return { transform: `scale(${1.25 - 0.25 * p})`, opacity: p };
  }
  return {};
};

// 主体在原片的水平位置 fx → object-position 百分比:让主体尽量落在框的正中。
// cover 时原片按高度撑满,横向多出 (sw - bw);窗口左边 = p × 多出的部分
export const objectX = (fx, srcAR, box) => {
  if (fx == null || !box) return 0.5;
  const sw = box.h * (srcAR || 16 / 9);
  if (sw <= box.w + 1) return 0.5;
  return Math.min(1, Math.max(0, (fx * sw - box.w / 2) / (sw - box.w)));
};

const Media = ({ clip, fps, cover = true, box = null }) => {
  const frame = useCurrentFrame();
  const total = Math.max(1, Math.round(clip.dur * fps));
  const p = Math.min(1, frame / total);
  // 去角标 / 去烧录字幕:四边收 crop_edge,底部再裁 crop_bottom
  const crop = 1 / Math.max(0.5, 1 - 2 * (clip.crop_edge || 0) - (clip.crop_bottom || 0));
  const shiftY = (clip.crop_bottom || 0) * 50;
  // 自动重构图:裁切窗口和推拉中心都对准主体(focus 由 Python 按边缘能量算出,缺省居中)
  const fx = `${(objectX(clip.focus, clip.ar, box) * 100).toFixed(1)}%`;
  const style = {
    width: "100%", height: "100%", objectFit: cover ? "cover" : "contain",
    objectPosition: `${fx} 50%`, transformOrigin: `${fx} 50%`,
    transform: `${motionTransform(clip.motion, p)} scale(${crop}) translateY(-${shiftY}%)`,
  };
  if (clip.kind === "still") return <Img src={src(clip.src)} style={style} />;
  return (
    <OffthreadVideo src={src(clip.src)} muted startFrom={Math.round((clip.src_in || 0) * fps)}
      style={style} />
  );
};

const Flash = ({ clip, fps }) => {
  const frame = useCurrentFrame();
  if (clip.transition !== "flash" && clip.transition !== "whip") return null;
  const peak = clip.transition === "flash" ? 1 : 0.35;
  const o = interpolate(frame / fps, [0, 0.22], [peak, 0], { extrapolateRight: "clamp" });
  return <AbsoluteFill style={{ background: "#fff", opacity: o }} />;
};

const BandClip = ({ clip, fps, box = null }) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      <AbsoluteFill style={transitionStyle(clip.transition, frame / fps)}>
        <Media clip={clip} fps={fps} box={box} />
      </AbsoluteFill>
      <Flash clip={clip} fps={fps} />
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- 叠加层
const popIn = (frame, fps, delay = 0, cfg = { damping: 11, stiffness: 200 }) =>
  spring({ frame: frame - Math.round(delay * fps), fps, config: cfg });

const Hook = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  const size = Math.round(band.height * 0.15);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center",
      background: "linear-gradient(180deg,rgba(0,0,0,0) 20%,rgba(0,0,0,.55) 100%)" }}>
      {o.props.lines.map((line, i) => {
        const p = popIn(frame, fps, i * 0.18, { damping: 9, stiffness: 240 });
        const shake = i === 0 ? Math.sin(frame * 1.7) * Math.max(0, 1 - frame / (fps * 0.4)) * 6 : 0;
        const first = fitSize(o.props.lines[0], size, band.w * 0.9, 0.2);
        const fs = i === 0 ? first : Math.min(fitSize(line, size * 0.6, band.w * 0.9, 0.7), first * 0.8);
        return (
          <div key={i} style={{
            fontFamily: FONT, fontWeight: 900, fontSize: fs, lineHeight: 1.25, textAlign: "center",
            padding: "0 5%", marginTop: i ? size * 0.12 : 0, opacity: Math.min(1, p * 1.5),
            transform: `translateX(${shake}px) scale(${interpolate(p, [0, 1], [1.6, 1])})`,
          }}>
            {i === 0
              ? <span style={{ color: "#fff", ...strokeText(fs, 0.08) }}>
                  {hotSpans(line).map((s, k) => <span key={k} style={{ color: s.hot ? YELLOW : undefined }}>{s.t}</span>)}
                </span>
              : <span style={plate(YELLOW, "#111", fs)}>{line}</span>}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

const Keyword = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  const size = Math.round(band.height * 0.13);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "flex-start", padding: "0 6%",
      background: "linear-gradient(90deg,rgba(0,0,0,.5) 0%,rgba(0,0,0,0) 70%)" }}>
      {o.props.lines.map((line, i) => {
        const p = popIn(frame, fps, i * 0.22);
        // 第二行永远比第一行小:第一行太长被缩小时,第二行跟着缩,主次不倒
        const first = fitSize(o.props.lines[0], size, band.w * 0.86, 0.7);
        const fs = i === 0 ? first : Math.min(fitSize(line, size * 0.66, band.w * 0.86, 0.7), first * 0.8);
        return (
          <div key={i} style={{
            fontFamily: FONT, fontWeight: 900, fontSize: fs, lineHeight: 1.3, marginTop: i ? size * 0.18 : 0,
            opacity: p, transform: `translateX(${interpolate(p, [0, 1], [-60, 0])}px) skewX(${(1 - p) * -8}deg)`,
          }}>
            <span style={i === 0 ? plate(accent, "#fff", fs) : plate(YELLOW, "#111", fs)}>{line}</span>
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

// "620km" → [620, 0 位小数, "", "km"];"1,210匹" → [1210, 0, "", "匹"]
export const splitNumber = (value) => {
  const m = String(value).match(/^([^\d-]*)(-?[\d,]+(?:\.\d+)?)(.*)$/);
  if (!m) return null;
  const num = parseFloat(m[2].replace(/,/g, ""));
  const dec = (m[2].split(".")[1] || "").length;
  return [num, dec, m[1], m[3]];
};

const Stat = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  const t = frame / fps - (o.props.at || 0);
  const parts = splitNumber(o.props.value);
  const p = interpolate(t, [0, 0.8], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic) });
  const shown = parts ? `${parts[2]}${(parts[0] * p).toFixed(parts[1])}${parts[3]}` : o.props.value;
  const size = Math.round(band.height * 0.24);
  const enter = popIn(frame, fps, 0);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center",
      background: "radial-gradient(ellipse at center,rgba(0,0,0,.35) 0%,rgba(0,0,0,.7) 100%)", opacity: enter }}>
      <div style={{ fontFamily: FONT, fontWeight: 900, fontSize: size * 0.3, letterSpacing: 2 }}>
        <span style={plate(accent, "#fff", size * 0.3)}>{o.props.label}</span></div>
      <div style={{ fontFamily: FONT, fontWeight: 900, fontSize: fitSize(o.props.value, size * 1.15, band.w * 0.9, 0.2),
        color: YELLOW, lineHeight: 1.15, ...strokeText(size * 1.15, 0.05),
        transform: `scale(${t > 0.8 ? 1 + 0.06 * Math.max(0, 1 - (t - 0.8) * 5) : 1})` }}>{shown}</div>
      {o.props.note && <div style={{ fontFamily: FONT, fontWeight: 700, fontSize: size * 0.2, color: "#eee",
        ...strokeText(size * 0.2, 0.06) }}>{o.props.note}</div>}
    </AbsoluteFill>
  );
};

const Panel = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  const size = Math.round(band.height * (band.land ? 0.045 : 0.062));
  const enter = popIn(frame, fps, 0, { damping: 16, stiffness: 160 });
  return (
    // 横屏:面板放左侧居中,避开底部字幕
    <AbsoluteFill style={{ justifyContent: band.land ? "center" : "flex-end",
      padding: band.land ? "0 0 0 4%" : "0 0 4% 5%" }}>
      <div style={{
        width: band.land ? "40%" : "62%", background: "rgba(10,12,16,.82)", borderLeft: `${size * 0.22}px solid ${accent}`,
        padding: `${size * 0.6}px ${size * 0.8}px`, fontFamily: FONT, color: "#fff",
        transform: `translateX(${interpolate(enter, [0, 1], [-110, 0])}%)`,
      }}>
        {o.props.title && <div style={{ fontWeight: 900, fontSize: size * 1.15, marginBottom: size * 0.4,
          color: accent }}>{o.props.title}</div>}
        {o.props.rows.map((r, i) => {
          const at = r.at != null ? r.at : 0.25 + i * 0.3;
          const rp = popIn(frame, fps, at);
          return (
            <div key={i} style={{ display: "flex", justifyContent: "space-between", fontSize: size,
              lineHeight: 1.5, opacity: rp, transform: `translateY(${(1 - rp) * 12}px)` }}>
              <span style={{ color: "#bbb" }}>{r.label}</span>
              <span style={{ fontWeight: 900 }}>{r.value}
                {r.basis && <span style={{ fontSize: size * 0.55, color: "#999", marginLeft: 8 }}>{r.basis}</span>}
              </span>
            </div>
          );
        })}
        {o.props.source && <div style={{ fontSize: size * 0.5, color: "#888", marginTop: size * 0.3 }}>
          {o.props.source}</div>}
      </div>
    </AbsoluteFill>
  );
};

const ChapterChip = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  const total = Math.round(o.dur * fps);
  const p = popIn(frame, fps, 0, { damping: 14, stiffness: 220 });
  const out = interpolate(frame, [total - 8, total], [1, 0], { extrapolateLeft: "clamp" });
  const size = Math.round(band.height * (band.land ? 0.05 : 0.07));
  return (
    <AbsoluteFill style={{ padding: "4% 5%", opacity: out }}>
      <div style={{ display: "inline-flex", alignItems: "center", fontFamily: FONT, fontWeight: 900,
        fontSize: size, color: "#fff", transform: `translateY(${(1 - p) * -30}px)`, opacity: p }}>
        <span style={{ background: accent, padding: `0 ${size * 0.35}px`, marginRight: size * 0.3 }}>
          {String(o.props.n).padStart(2, "0")}</span>
        <span style={{ background: "rgba(0,0,0,.7)", padding: `0 ${size * 0.4}px` }}>{o.props.name}</span>
      </div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- 数据卡(spec / compare / bars / timeline)
// 数字都在分镜阶段逐行核对过事实;行带 at(旁白念到它的秒数)就按旁白节奏出行
const rowAt = (r, i, step = 0.4) => (r.at != null ? r.at : 0.35 + i * step);

const Card = ({ o, accent, fps, band, theme }) => {
  const frame = useCurrentFrame();
  const t = frame / fps;
  const p = o.props;
  const a2 = theme.accent2 || "#FFB547";
  // 行少时字放大:一两行的对比卡数字才是主角
  const size = Math.round(band.height * (band.land ? 0.72 : 1)
    * (p.rows.length <= 2 ? 0.088 : p.rows.length <= 3 ? 0.074 : 0.064));
  const enter = popIn(frame, fps, 0, { damping: 16, stiffness: 170 });
  const total = Math.round(o.dur * fps);
  const out = interpolate(frame, [total - 6, total], [1, 0], { extrapolateLeft: "clamp" });
  const maxNum = Math.max(1e-9, ...p.rows.map((r) => Math.abs(r.num || 0)));
  const row = (r, i) => {
    const at = rowAt(r, i, p.kind === "compare" ? 0.6 : 0.4);
    const rp = popIn(frame, fps, at);
    const base = { display: "flex", alignItems: "baseline", fontSize: size, lineHeight: 1.55,
      opacity: Math.min(1, rp * 1.4), transform: `translateY(${(1 - rp) * 14}px)` };
    const tag = r.tag && <div style={{ fontSize: size * 0.48, color: "#8b93a1", lineHeight: 1.1 }}>{r.tag}</div>;
    if (p.kind === "compare") {
      const rp2 = popIn(frame, fps, at + 0.28);
      return (
        <div key={i} style={{ ...base, flexDirection: "column", alignItems: "stretch" }}>
          <div style={{ fontSize: size * 0.62, color: "#aeb6c2", textAlign: "center" }}>{r.label}</div>
          <div style={{ display: "flex" }}>
            <div style={{ flex: 1, textAlign: "right", paddingRight: size * 0.45, fontWeight: 900, color: accent,
              fontSize: size * 1.25 }}>{r.values[0]}</div>
            <div style={{ alignSelf: "center", fontSize: size * 0.42, fontWeight: 900, color: "#111",
              background: YELLOW, borderRadius: "50%", width: size * 0.95, height: size * 0.95, lineHeight: `${size * 0.95}px`,
              textAlign: "center", flex: "none", fontStyle: "italic" }}>VS</div>
            <div style={{ flex: 1, paddingLeft: size * 0.45, fontWeight: 900, color: a2, opacity: rp2,
              fontSize: size * 1.25, transform: `translateX(${(1 - rp2) * 30}px)` }}>{r.values[1]}</div>
          </div>
          {r.tag && <div style={{ fontSize: size * 0.48, color: "#8b93a1", lineHeight: 1.1, textAlign: "center" }}>{r.tag}</div>}
        </div>
      );
    }
    if (p.kind === "bars") {
      const grow = interpolate(t - at, [0, 0.7], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp",
        easing: Easing.out(Easing.cubic) });
      const w = Math.abs(r.num || 0) / maxNum;
      return (
        <div key={i} style={{ ...base, flexDirection: "column", alignItems: "stretch", lineHeight: 1.25, marginBottom: size * 0.25 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: size * 0.8 }}>
            <span style={{ color: "#cfd5de" }}>{r.label}</span><span style={{ fontWeight: 900 }}>{r.values[0]}</span>
          </div>
          <div style={{ height: size * 0.42, background: "rgba(255,255,255,.1)" }}>
            <div style={{ width: `${w * grow * 100}%`, height: "100%", background: i === 0 ? accent : a2 }} />
          </div>
        </div>
      );
    }
    if (p.kind === "timeline") {
      return (
        <div key={i} style={{ ...base, alignItems: "center" }}>
          <div style={{ width: size * 0.4, height: size * 0.4, borderRadius: "50%", background: accent,
            marginRight: size * 0.5, flex: "none", boxShadow: `0 0 0 ${size * 0.12}px rgba(255,255,255,.12)` }} />
          <div style={{ fontWeight: 900, color: accent, minWidth: "30%" }}>{r.label}</div>
          <div style={{ flex: 1 }}>{r.values[0]}{tag}</div>
        </div>
      );
    }
    return (
      <div key={i} style={{ ...base, justifyContent: "space-between", borderBottom: "1px solid rgba(255,255,255,.12)" }}>
        <span style={{ color: "#bcc4cf" }}>{r.label}</span>
        <span style={{ textAlign: "right", fontWeight: 900 }}>{r.values[0]}{tag}</span>
      </div>
    );
  };
  return (
    // 横屏:卡片靠上,底部 ~20% 留给字幕
    <AbsoluteFill style={{ background: "rgba(5,7,10,.45)", alignItems: "center", opacity: out,
      justifyContent: band.land ? "flex-start" : "center", paddingTop: band.land ? "6%" : 0 }}>
      <div style={{
        width: band.land ? "62%" : "86%", maxHeight: band.land ? "72%" : "92%", overflow: "hidden",
        fontFamily: FONT, color: "#fff",
        background: "rgba(14,17,23,.88)", borderTop: `${size * 0.16}px solid ${accent}`,
        borderRadius: Math.round(size * 0.18), boxShadow: "0 18px 50px rgba(0,0,0,.45)",
        padding: `${size * 0.6}px ${size * 0.9}px ${size * 0.5}px`,
        transform: `scale(${interpolate(enter, [0, 1], [0.92, 1])})`, opacity: enter,
      }}>
        {p.title && <div style={{ fontWeight: 900, fontSize: size * 1.15, marginBottom: size * 0.35,
          whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{p.title}</div>}
        {p.kind === "compare" && p.columns.length === 2 && (
          <div style={{ display: "flex", fontSize: size * 0.75, fontWeight: 900, marginBottom: size * 0.2 }}>
            <div style={{ flex: 1, textAlign: "right", paddingRight: size * 0.95, color: accent }}>{p.columns[0]}</div>
            <div style={{ flex: 1, paddingLeft: size * 0.95, color: a2 }}>{p.columns[1]}</div>
          </div>
        )}
        {p.rows.map(row)}
        {p.source && <div style={{ fontSize: size * 0.44, color: "#7d8592", marginTop: size * 0.35 }}>{p.source}</div>}
      </div>
    </AbsoluteFill>
  );
};

const OVERLAYS = { hook: Hook, keyword: Keyword, stat: Stat, panel: Panel, chapter: ChapterChip, card: Card };

// ---------------------------------------------------------------- 顶部标题带 + 章节进度
const TitleBar = ({ title, chapters, duration, accent, box, width }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const now = frame / fps;
  const maxSize = box.height * 0.36;
  // 一行时字号能到最大字号的七成以上就不拆行
  const perLine = Math.max(6, Math.floor((width * 0.9) / (maxSize * 0.7)));
  const lines = splitTitle(title, perLine);
  const longest = Math.max(...lines.map((l) => l.length), 1);
  const size = Math.round(Math.min(maxSize, (width * 0.9) / longest, lines.length > 1 ? box.height * 0.33 : maxSize));
  return (
    <div style={{ position: "absolute", top: box.top, height: box.height, left: "5%", right: "5%",
      display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center" }}>
      {lines.map((line, i) => (
        <div key={i} style={{ fontFamily: FONT, fontWeight: 900, fontSize: size, color: "#fff", lineHeight: 1.18,
          textAlign: "center", whiteSpace: "nowrap", ...strokeText(size, 0.07) }}>
          {hotSpans(line).map((s, k) => <span key={k} style={{ color: s.hot ? YELLOW : undefined }}>{s.t}</span>)}
        </div>
      ))}
      <div style={{ display: "flex", gap: 8, marginTop: size * 0.32, width: "100%" }}>
        {chapters.map((c, i) => {
          const p = Math.max(0, Math.min(1, (now - c.start) / Math.max(0.01, c.end - c.start)));
          return (
            <div key={i} style={{ flex: (c.end - c.start) / duration, height: 10, borderRadius: 5,
              background: "rgba(255,255,255,.28)", overflow: "hidden" }}>
              <div style={{ width: `${p * 100}%`, height: "100%", background: accent }} />
            </div>
          );
        })}
      </div>
    </div>
  );
};

// 横屏底部章节进度条(竖屏的进度在标题下面)
const ProgressBar = ({ chapters, duration, accent, now }) => (
  <div style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: 8, display: "flex", gap: 4 }}>
    {chapters.map((c, i) => {
      const p = Math.max(0, Math.min(1, (now - c.start) / Math.max(0.01, c.end - c.start)));
      return (
        <div key={i} style={{ flex: (c.end - c.start) / Math.max(0.01, duration), background: "rgba(255,255,255,.3)" }}>
          <div style={{ width: `${p * 100}%`, height: "100%", background: accent }} />
        </div>
      );
    })}
  </div>
);

// 关键词冲击推:念到数字 / 车型代号时素材带快速放大 7% 再回落(0.35 秒)
export const punchScale = (punches, now) => {
  let s = 1;
  for (const t0 of punches) {
    const k = (now - t0) / 0.35;
    if (k >= 0 && k < 1) s = Math.max(s, 1 + 0.07 * (1 - k) * (1 - k));
  }
  return s;
};

// ---------------------------------------------------------------- 整片
export const Edit = (props) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const now = frame / fps;
  const g = grid(width, height);
  const theme = props.theme || {};
  const accent = theme.accent || props.accent || "#FF5A1F";
  const clips = props.clips || [];
  const current = clips.find((c) => now >= c.start && now < c.start + c.dur) || clips[clips.length - 1];
  const fullScreen = current && current.kind === "beat";
  const seq = (start, dur) => ({ from: Math.round(start * fps), durationInFrames: Math.max(1, Math.round(dur * fps)) });
  const bandStyle = { position: "absolute", left: 0, width: "100%", top: g.band.top, height: g.band.height, overflow: "hidden",
    boxShadow: "0 0 60px rgba(0,0,0,.55)" };
  useEmbeddedFont(props.font);

  return (
    <AbsoluteFill style={{ background: "#0b0d10" }}>
      {/* 竖屏底:当前素材的模糊版,避免大片纯黑。用 Python 预先抽帧模糊好的小图(clip.backdrop),
          实时 CSS 模糊整屏视频会让渲染慢 2–3 倍 */}
      {g.title && clips.filter((c) => c.kind !== "beat" && c.backdrop).map((c, i) => (
        <Sequence key={`bg${i}`} {...seq(c.start, c.dur)}>
          <AbsoluteFill>
            <Img src={src(c.backdrop)} style={{ width: "100%", height: "100%", objectFit: "cover",
              transform: "scale(1.1)" }} />
          </AbsoluteFill>
        </Sequence>
      ))}
      {/* 压暗底图:上下两端更暗,标题和字幕更跳,底部留白看起来是有意的 */}
      {g.title && <AbsoluteFill style={{ background:
        "linear-gradient(180deg,rgba(0,0,0,.45) 0%,rgba(0,0,0,.15) 30%,rgba(0,0,0,.25) 70%,rgba(0,0,0,.7) 100%)" }} />}
      {g.title && !fullScreen && (
        <TitleBar title={props.title} chapters={props.chapters || []} duration={props.duration}
          accent={accent} box={g.title} width={width} />
      )}
      <div style={bandStyle}>
        <AbsoluteFill style={{ transform: `scale(${punchScale(props.punches || [], now)})` }}>
          {clips.filter((c) => c.kind !== "beat").map((c, i) => (
            <Sequence key={`c${i}`} {...seq(c.start, c.dur)}>
              <BandClip clip={c} fps={fps} box={{ w: width, h: g.band.height }} />
            </Sequence>
          ))}
        </AbsoluteFill>
        {(props.overlays || []).map((o, i) => {
          const C = OVERLAYS[o.type];
          return C ? (
            <Sequence key={`o${i}`} {...seq(o.start, o.dur)}>
              <C o={o} accent={accent} fps={fps} band={{ ...g.band, w: width, land: !g.title }} theme={theme} />
            </Sequence>
          ) : null;
        })}
      </div>
      {clips.filter((c) => c.kind === "beat").map((c, i) => (
        <Sequence key={`b${i}`} {...seq(c.start, c.dur)}>
          <BandClip clip={{ ...c, motion: "none" }} fps={fps} />
        </Sequence>
      ))}
      {!g.title && !fullScreen && <ProgressBar chapters={props.chapters || []} duration={props.duration}
        accent={accent} now={now} />}
      <AbsoluteFill>
        {(props.captions || []).filter((c) => now >= c.start && now < c.end).map((c, i) => (
          <Cue key={i} cue={c} style={{ accent, bottom: g.capBottom, size: g.title ? 0.073 : 0.06,
            box: theme.subs === "outline" ? undefined : theme.subs, boxColor: theme.subBox }} />
        ))}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
