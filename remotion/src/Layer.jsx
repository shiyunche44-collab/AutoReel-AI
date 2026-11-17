import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, spring, interpolate } from "remotion";

const FONT = '"Noto Sans SC Black","Microsoft YaHei",sans-serif';

// 一条字幕:整句白字描边,关键词(hot)变强调色并弹出放大;整句 0.12s 弹入。
// style.box:"box" = 半透明黑底条、"band" = 色块底(style.boxColor),不描边;缺省 = 描边无底
export const Cue = ({ cue, style }) => {
  const frame = useCurrentFrame();
  const { fps, height } = useVideoConfig();
  const t = frame / fps - cue.start;
  const pop = spring({ frame: Math.max(0, t * fps), fps, config: { damping: 12, stiffness: 220 } });
  const size = Math.round(Math.min(height, 1080) * (style.size || 0.062));
  const accent = style.accent || "#FF5A1F";
  const boxed = style.box === "box" || style.box === "band";
  const hotColor = style.box === "band" ? "#FFE14D" : accent;
  return (
    <div style={{
      position: "absolute", left: "6%", right: "6%", bottom: `${(style.bottom || 0.11) * 100}%`,
      textAlign: "center", fontFamily: FONT, fontSize: size, fontWeight: 900, lineHeight: 1.18,
      color: "#fff", letterSpacing: 1,
      WebkitTextStroke: boxed ? undefined : `${Math.max(3, size / 10)}px #000`, paintOrder: "stroke fill",
      transform: `scale(${interpolate(pop, [0, 1], [0.86, 1])})`, opacity: Math.min(1, pop * 1.6),
    }}>
      <span style={boxed ? {
        background: style.box === "band" ? (style.boxColor || accent) : "rgba(0,0,0,.62)",
        padding: "0.06em 0.4em", borderRadius: size * 0.12, WebkitBoxDecorationBreak: "clone",
        boxDecorationBreak: "clone",
      } : undefined}>
      {cue.words.map((w, i) => {
        const hotT = Math.max(0, t - w.start + cue.start);
        const hp = w.hot ? spring({ frame: hotT * fps, fps, config: { damping: 9, stiffness: 260 } }) : 0;
        return (
          <span key={i} style={{
            color: w.hot ? hotColor : "#fff", display: "inline-block",
            margin: w.hot ? "0 0.06em" : undefined,
            transform: w.hot ? `scale(${1 + 0.14 * hp})` : undefined,
          }}>{w.text}</span>
        );
      })}
      </span>
    </div>
  );
};

export const Layer = ({ cues, style }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const now = frame / fps;
  return (
    <AbsoluteFill>
      {cues.filter((c) => now >= c.start && now < c.end).map((c, i) => (
        <Cue key={i} cue={c} style={style || {}} />
      ))}
    </AbsoluteFill>
  );
};
