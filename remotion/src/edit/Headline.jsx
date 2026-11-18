// commercial 大字:钩子(开场)、关键句 / 关键数字(左上角粗斜体描边)、大数字(压暗实拍上滚数)。
// 字都在 Python 端挑好(edl.commercial_overlays,数据在分镜阶段核对过),这里只管样子和节奏。
import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";
import {
  FONT, clamp01, countUp, ease, fitSize, hotSpans, inOut, plate, popIn, strokeText,
} from "./theme.js";

const SKEW = "skewX(-9deg)"; // 粗斜体:字体没有斜体字形,统一用斜切,各家字体效果一致

// 数字 / 型号上强调色
const Hot = ({ text, accent }) => hotSpans(text).map((s, k) => (
  <span key={k} style={{ color: s.hot ? accent : undefined }}>{s.t}</span>
));

// 眉题:强调色短竖条 + 小号白字
const Kicker = ({ text, size, accent, p }) => (
  <div style={{ display: "flex", alignItems: "center", fontFamily: FONT, fontWeight: 900,
    fontSize: size, color: "#fff", marginBottom: size * 0.35, opacity: p,
    transform: `translateX(${(1 - p) * -24}px) ${SKEW}`, ...strokeText(size, 0.05) }}>
    <span style={{ display: "inline-block", width: size * 0.22, height: size * 1.05, background: accent,
      marginRight: size * 0.35, transform: `scaleY(${p})` }} />
    {text}
  </div>
);

// ------------------------------------------------------------------ 钩子:开场砸进来的大字
export const CHook = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  const t = frame / fps;
  const lines = o.props.lines || [];
  const size = Math.round(band.height * 0.15);
  const maxW = band.w * 0.84;
  const first = fitSize(lines[0], size, maxW, 0.3);
  const out = inOut(t, o.dur, 0.001, 0.22);
  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", paddingBottom: "6%",
      background: "radial-gradient(ellipse at 50% 45%,rgba(0,0,0,.35) 0%,rgba(0,0,0,.6) 100%)",
      opacity: out }}>
      {lines.map((line, i) => {
        // 第一行 0.18 秒砸下来(放大 + 模糊收拢),第二行晚 0.2 秒弹出
        const k = i === 0 ? ease(clamp01(t / 0.18)) : popIn(frame, fps, 0.2, { damping: 12, stiffness: 230 });
        const fs = i === 0 ? first : Math.min(fitSize(line, size * 0.5, maxW, 0.7), first * 0.6);
        return (
          <div key={i} style={{
            fontFamily: FONT, fontWeight: 900, fontSize: fs, lineHeight: 1.22, textAlign: "center",
            marginTop: i ? fs * 0.35 : 0, whiteSpace: "nowrap",
            opacity: clamp01(k * 1.4),
            filter: i === 0 && k < 1 ? `blur(${((1 - k) * 8).toFixed(1)}px)` : undefined,
            transform: `scale(${i === 0 ? interpolate(k, [0, 1], [1.45, 1]) : interpolate(k, [0, 1], [0.7, 1])}) ${SKEW}`,
          }}>
            {i === 0
              ? <span style={{ color: "#fff", ...strokeText(fs, 0.07) }}><Hot text={line} accent={accent} /></span>
              : <span style={plate(accent, "#14120c", fs)}>{line}</span>}
          </div>
        );
      })}
    </AbsoluteFill>
  );
};

// ------------------------------------------------------------------ 关键句 / 关键数字:左上角
export const Headline = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  const t = frame / fps;
  const lines = o.props.lines || [];
  const size = Math.round(band.height * (band.land ? 0.105 : 0.085));
  const maxW = band.w * (band.land ? 0.62 : 0.86);
  const first = fitSize(lines[0], size, maxW, 0.3);
  const out = inOut(t, o.dur, 0.001, 0.22);
  const glow = popIn(frame, fps, 0, { damping: 20, stiffness: 160 });
  return (
    <AbsoluteFill style={{ opacity: out }}>
      {/* 左上角压暗一点,白字压在亮天空上也清楚 */}
      <AbsoluteFill style={{ opacity: glow,
        background: "radial-gradient(ellipse 70% 60% at 0% 0%,rgba(0,0,0,.5) 0%,rgba(0,0,0,0) 100%)" }} />
      <div style={{ position: "absolute", left: "6%", top: band.land ? "8%" : "6%" }}>
        {o.props.kicker && (
          <Kicker text={o.props.kicker} size={Math.round(first * 0.34)} accent={accent}
            p={popIn(frame, fps, 0, { damping: 16, stiffness: 220 })} />
        )}
        {lines.map((line, i) => {
          const p = popIn(frame, fps, 0.06 + i * 0.1, { damping: 13, stiffness: 210 });
          const fs = i === 0 ? first : Math.min(fitSize(line, size * 0.5, maxW, 0.3), first * 0.56);
          return (
            <div key={i} style={{
              fontFamily: FONT, fontWeight: 900, fontSize: fs, lineHeight: 1.18, color: "#fff",
              whiteSpace: "nowrap", marginTop: i ? fs * 0.2 : 0, opacity: clamp01(p * 1.3),
              transform: `translateX(${(1 - p) * -60}px) ${SKEW}`, transformOrigin: "0 100%",
              ...strokeText(fs, i === 0 ? 0.075 : 0.08),
            }}>
              <Hot text={line} accent={accent} />
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

// ------------------------------------------------------------------ 大数字:压暗的实拍上滚数
export const BigStat = ({ o, accent, fps, band }) => {
  const frame = useCurrentFrame();
  const t = frame / fps;
  const at = o.props.at || 0;
  const roll = ease(clamp01((t - at) / 0.8));
  const size = Math.round(band.height * (band.land ? 0.22 : 0.17));
  const maxW = band.w * (band.land ? 0.7 : 0.88);
  const fs = fitSize(o.props.value, size, maxW, 0.3);
  const enter = popIn(frame, fps, 0, { damping: 18, stiffness: 170 });
  const out = inOut(t, o.dur, 0.001, 0.25);
  // 滚到位时轻轻顶一下
  const settle = t > at + 0.8 ? 1 + 0.05 * Math.max(0, 1 - (t - at - 0.8) * 5) : 1;
  const label = Math.round(fs * 0.24);
  return (
    <AbsoluteFill style={{ opacity: out }}>
      <AbsoluteFill style={{ opacity: enter,
        background: "linear-gradient(90deg,rgba(0,0,0,.72) 0%,rgba(0,0,0,.45) 55%,rgba(0,0,0,.2) 100%)" }} />
      <div style={{ position: "absolute", left: "7%", top: 0, bottom: band.land ? "16%" : 0,
        display: "flex", flexDirection: "column", justifyContent: "center" }}>
        {o.props.label && <Kicker text={o.props.label} size={label} accent={accent} p={enter} />}
        <div style={{ fontFamily: FONT, fontWeight: 900, fontSize: fs, lineHeight: 1.05, color: accent,
          whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums",
          transform: `scale(${settle}) ${SKEW}`, transformOrigin: "0 70%", opacity: enter,
          ...strokeText(fs, 0.04) }}>
          {countUp(o.props.value, roll)}
        </div>
        {o.props.note && (
          <div style={{ fontFamily: FONT, fontWeight: 900, fontSize: label * 0.85, color: "#fff",
            marginTop: label * 0.4, opacity: clamp01((t - at - 0.5) / 0.3),
            transform: SKEW, ...strokeText(label * 0.85, 0.06) }}>{o.props.note}</div>
        )}
      </div>
    </AbsoluteFill>
  );
};
