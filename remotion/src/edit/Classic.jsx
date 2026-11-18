// classic 包装(花字关键词、侧面板、章节小标、数据卡、竖屏标题带、横屏进度条、关键词冲击推)。
// 从 Edit.jsx 原样拆出,行为不变。
import React from "react";
import { AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import {
  FONT, YELLOW, fitSize, hotSpans, plate, popIn, splitNumber, splitTitle, strokeText,
} from "./theme.js";

export const Hook = ({ o, accent, fps, band }) => {
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

export const Keyword = ({ o, accent, fps, band }) => {
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

export const Stat = ({ o, accent, fps, band }) => {
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

export const Panel = ({ o, accent, fps, band }) => {
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

export const ChapterChip = ({ o, accent, fps, band }) => {
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

export const Card = ({ o, accent, fps, band, theme }) => {
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

export const CLASSIC_OVERLAYS = { hook: Hook, keyword: Keyword, stat: Stat, panel: Panel, chapter: ChapterChip, card: Card };

// ---------------------------------------------------------------- 顶部标题带 + 章节进度
export const TitleBar = ({ title, chapters, duration, accent, box, width }) => {
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
export const ProgressBar = ({ chapters, duration, accent, now }) => (
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
