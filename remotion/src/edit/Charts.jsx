// commercial 整屏图表:纸纹浅底 / 深色纹理底,参数表(spec)、对比(compare)、条形(bars)、时间线(timeline)。
// 数字都在分镜阶段逐行核对过事实(cards.ground / edl.panel_chart);行带 at(旁白念到它的秒数)就按旁白出行。
// 横屏底部 ~20% 留给字幕。进场是快速推镜 + 模糊收拢。
import React from "react";
import { AbsoluteFill, useCurrentFrame } from "remotion";
import { FONT, clamp01, countUp, ease, fitSize, inOut, popIn, splitNumber, textEm } from "./theme.js";

const grain = (alpha, tone) => {
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' width='220' height='220'>"
    + "<filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/>"
    + `<feColorMatrix values='0 0 0 0 ${tone} 0 0 0 0 ${tone} 0 0 0 0 ${tone} 0 0 0 ${alpha} 0'/></filter>`
    + "<rect width='100%' height='100%' filter='url(#n)'/></svg>";
  return `url("data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}")`;
};

const PALETTES = {
  paper: {
    bg: `${grain(0.09, 0)}, radial-gradient(ellipse 90% 80% at 38% 30%,#F7F4EE 0%,#ECE7DE 62%,#DFD9CE 100%)`,
    ink: "#1C1B19", muted: "#6E6961", rule: "rgba(28,27,25,.16)", track: "rgba(28,27,25,.08)",
    neutral: "#4A4741",
  },
  dark: {
    bg: `${grain(0.06, 1)}, radial-gradient(ellipse 90% 80% at 38% 30%,#1F2228 0%,#131519 65%,#0B0C0E 100%)`,
    ink: "#F4F2EC", muted: "#9AA0AA", rule: "rgba(255,255,255,.14)", track: "rgba(255,255,255,.08)",
    neutral: "#C9CDD4",
  },
};

const rowAt = (r, i, step) => (r.at != null ? r.at : 0.45 + i * step);
const numOf = (v) => {
  const parts = splitNumber(v);
  return parts ? Math.abs(parts[0]) : null;
};

export const Chart = ({ o, fps, band, theme }) => {
  const frame = useCurrentFrame();
  const t = frame / fps;
  const p = o.props;
  const c = theme.chart || {};
  const pal = PALETTES[c.bg === "dark" ? "dark" : "paper"];
  const a1 = c.accent || theme.accent || "#DE3E1E";
  const a2 = c.accent2 || theme.accent2 || "#1E5CA8";
  const H = band.height;
  const W = band.w;
  const n = Math.max(1, p.rows.length);
  // 内容区:上 9%,下留 20% 给字幕,左右各 8%
  const top = H * (band.land ? 0.09 : 0.06);
  const bottom = H * (band.land ? 0.2 : 0.08);
  const left = W * 0.08;
  const innerW = W - left * 2;
  const title = Math.round(H * (band.land ? 0.06 : 0.05));
  const note = Math.round(Math.max(14, H * 0.018)); // 来源一行放在内容区底部
  const avail = H - top - bottom - title * 1.9 - (p.source ? note * 2 : 0);
  const perRow = { spec: 2.0, compare: 3.0, bars: 2.3, timeline: 2.0 }[p.kind] || 2.0;
  const s = Math.round(Math.min(H * (band.land ? 0.062 : 0.04), avail / (n * perRow + (p.kind === "compare" ? 1.2 : 0))));

  // 进场:0.28 秒从 1.06 倍推到 1 倍,模糊收拢;出场 0.2 秒淡出
  const enter = ease(clamp01(t / 0.28));
  const out = inOut(t, o.dur, 0.001, 0.2);

  const reveal = (i, step) => {
    const at = rowAt(p.rows[i], i, step);
    return { at, k: popIn(frame, fps, at, { damping: 17, stiffness: 190 }) };
  };
  const roll = (at) => ease(clamp01((t - at) / 0.6));
  // 同一张图里同一列字号一致(各行按最长的那行缩)
  const same = (texts, size, maxW, extra = 0) => Math.min(size, ...texts.map((x) => fitSize(String(x ?? ""), size, maxW, extra)));

  const rowsSpec = () => {
    const vfs = same(p.rows.map((r) => r.values[0]), s * 1.3, innerW * 0.5, 0.2);
    const lfs = same(p.rows.map((r) => r.label), s * 0.82, innerW * 0.42);
    return p.rows.map((r, i) => {
    const { at, k } = reveal(i, 0.45);
    const value = String(r.values[0] ?? "");
    return (
      <div key={i} style={{ position: "relative", display: "flex", alignItems: "baseline",
        justifyContent: "space-between", height: s * perRow, paddingTop: s * 0.35,
        opacity: clamp01(k * 1.4), transform: `translateY(${(1 - k) * s * 0.4}px)` }}>
        <div style={{ fontSize: lfs, color: pal.muted, fontWeight: 900,
          whiteSpace: "nowrap" }}>{r.label}</div>
        <div style={{ textAlign: "right", whiteSpace: "nowrap" }}>
          <span style={{ fontSize: vfs, fontWeight: 900, color: i === 0 ? a1 : pal.ink,
            fontVariantNumeric: "tabular-nums" }}>{countUp(value, roll(at))}</span>
          {r.tag && <div style={{ fontSize: s * 0.42, color: pal.muted, marginTop: s * 0.05 }}>{r.tag}</div>}
        </div>
        <div style={{ position: "absolute", left: 0, bottom: 0, height: Math.max(2, s * 0.04),
          width: `${ease(clamp01((t - at) / 0.45)) * 100}%`, background: pal.rule }} />
      </div>
    );
    });
  };

  const rowsCompare = () => {
    const nums = p.rows.map((r) => [numOf(r.values[0]), numOf(r.values[1])]);
    const vfs = same(p.rows.flatMap((r) => r.values.slice(0, 2)), s * 1.35, innerW * 0.34);
    const lfs = same(p.rows.map((r) => r.label), s * 0.72, innerW * 0.2);
    return p.rows.map((r, i) => {
      const { at, k } = reveal(i, 0.7);
      const k2 = popIn(frame, fps, at + 0.25, { damping: 17, stiffness: 190 });
      const [x, y] = nums[i];
      const max = Math.max(x || 0, y || 0, 1e-9);
      const grow = ease(clamp01((t - at) / 0.6));
      const bar = (v, color, side, kk) => (x != null && y != null ? (
        <div style={{ flex: 1, display: "flex", justifyContent: side === "l" ? "flex-end" : "flex-start" }}>
          <div style={{ width: `${(v / max) * 100 * grow * kk}%`, height: s * 0.26, background: color,
            borderRadius: s * 0.04 }} />
        </div>) : <div style={{ flex: 1 }} />);
      return (
        <div key={i} style={{ height: s * perRow, display: "flex", flexDirection: "column", justifyContent: "center",
          opacity: clamp01(k * 1.4), transform: `translateY(${(1 - k) * s * 0.4}px)` }}>
          <div style={{ display: "flex", alignItems: "baseline" }}>
            <div style={{ flex: 1, textAlign: "right", fontSize: vfs, fontWeight: 900, color: a1,
              fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{countUp(r.values[0], roll(at))}</div>
            <div style={{ width: innerW * 0.22, textAlign: "center", fontSize: lfs,
              color: pal.muted, fontWeight: 900, whiteSpace: "nowrap" }}>{r.label}</div>
            <div style={{ flex: 1, fontSize: vfs, fontWeight: 900, color: a2, opacity: clamp01(k2 * 1.4),
              fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{countUp(r.values[1], roll(at + 0.25))}</div>
          </div>
          <div style={{ display: "flex", alignItems: "center", marginTop: s * 0.22 }}>
            {bar(x || 0, a1, "l", 1)}
            <div style={{ width: innerW * 0.22 }} />
            {bar(y || 0, a2, "r", k2)}
          </div>
          {r.tag && (
            <div style={{ textAlign: "center", marginTop: s * 0.18, whiteSpace: "nowrap", color: pal.muted,
              fontSize: fitSize(r.tag, s * 0.4, innerW * 0.7), opacity: clamp01((t - at - 0.5) / 0.3) }}>{r.tag}</div>
          )}
        </div>
      );
    });
  };

  const rowsBars = () => {
    const nums = p.rows.map((r) => (r.num != null ? Math.abs(r.num) : numOf(r.values[0]) || 0));
    const max = Math.max(1e-9, ...nums);
    const labelW = innerW * 0.24;
    const lfs = same(p.rows.map((r) => r.label), s * 0.82, labelW - s * 0.5);
    // 柱长按像素算、不许收缩:数值 + 口径小字放柱子右边(口径在数值下面),先扣掉最宽那一行的字
    const tfs = s * 0.42;
    const textW = Math.max(...p.rows.map((r) => Math.max(textEm(r.values[0]) * s * 1.05, textEm(r.tag) * tfs)));
    const barMax = Math.max(innerW * 0.22, innerW - labelW - s * 0.5 - Math.min(textW, innerW * 0.4));
    return p.rows.map((r, i) => {
      const { at, k } = reveal(i, 0.4);
      const grow = ease(clamp01((t - at) / 0.7));
      const color = i === 0 ? a1 : pal.neutral;
      return (
        <div key={i} style={{ height: s * perRow, display: "flex", alignItems: "center",
          opacity: clamp01(k * 1.4) }}>
          <div style={{ width: labelW, flexShrink: 0, paddingRight: s * 0.5, textAlign: "right", fontWeight: 900,
            fontSize: lfs, color: i === 0 ? pal.ink : pal.muted,
            whiteSpace: "nowrap" }}>{r.label}</div>
          <div style={{ width: (nums[i] / max) * barMax * grow, flexShrink: 0, height: s * 0.9, background: color,
            borderRadius: `0 ${s * 0.08}px ${s * 0.08}px 0` }} />
          <div style={{ marginLeft: s * 0.4, minWidth: 0, display: "flex", flexDirection: "column" }}>
            <div style={{ fontSize: s * 1.05, lineHeight: 1.05, fontWeight: 900, color: i === 0 ? a1 : pal.ink,
              fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>{countUp(r.values[0], grow)}</div>
            {r.tag && <div style={{ fontSize: fitSize(r.tag, tfs, innerW * 0.4), color: pal.muted, fontWeight: 700,
              whiteSpace: "nowrap", marginTop: s * 0.08, opacity: clamp01((t - at - 0.5) / 0.3) }}>{r.tag}</div>}
          </div>
        </div>
      );
    });
  };

  const rowsTimeline = () => {
    const last = rowAt(p.rows[n - 1], n - 1, 0.45);
    const line = ease(clamp01((t - 0.3) / Math.max(0.5, last)));
    return (
      <div style={{ position: "relative" }}>
        <div style={{ position: "absolute", left: s * 0.22, top: s * 0.5, width: Math.max(2, s * 0.06),
          height: `${line * 100}%`, maxHeight: s * perRow * (n - 0.5), background: pal.rule }} />
        {p.rows.map((r, i) => {
          const { k } = reveal(i, 0.45);
          return (
            <div key={i} style={{ height: s * perRow, display: "flex", alignItems: "flex-start",
              opacity: clamp01(k * 1.4), transform: `translateX(${(1 - k) * s * 0.6}px)` }}>
              <div style={{ width: s * 0.5, height: s * 0.5, borderRadius: "50%", background: a1, flex: "none",
                marginTop: s * 0.3, transform: `scale(${k})` }} />
              <div style={{ width: innerW * 0.22, paddingLeft: s * 0.6, fontWeight: 900, fontSize: s * 0.95,
                color: a1, whiteSpace: "nowrap" }}>{r.label}</div>
              <div style={{ flex: 1, fontSize: fitSize(r.values[0], s * 0.95, innerW * 0.7), fontWeight: 900,
                color: pal.ink }}>{r.values[0]}
                {r.tag && <div style={{ fontSize: s * 0.42, color: pal.muted, fontWeight: 400 }}>{r.tag}</div>}
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  const body = { spec: rowsSpec, compare: rowsCompare, bars: rowsBars, timeline: rowsTimeline }[p.kind] || rowsSpec;
  const titleFs = fitSize(p.title || "", title, innerW * 0.9, 0.6);
  return (
    <AbsoluteFill style={{ opacity: out }}>
      <AbsoluteFill style={{ background: pal.bg, transform: `scale(${1.06 - 0.06 * enter})`,
        filter: enter < 1 ? `blur(${((1 - enter) * 10).toFixed(1)}px)` : undefined, opacity: clamp01(enter * 2) }}>
        <div style={{ position: "absolute", left, right: left, top, bottom: bottom + (p.source ? note * 2 : 0),
          fontFamily: FONT, color: pal.ink, display: "flex", flexDirection: "column" }}>
          {p.title && (
            <div style={{ display: "flex", alignItems: "center", fontSize: titleFs, fontWeight: 900,
              lineHeight: 1.2, marginBottom: title * 0.7, whiteSpace: "nowrap" }}>
              <span style={{ display: "inline-block", width: titleFs * 0.2, height: titleFs * 0.95, background: a1,
                marginRight: titleFs * 0.35, transform: `scaleY(${enter})` }} />
              {p.title}
            </div>
          )}
          <div style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center",
            paddingBottom: title * 0.6 }}>
          {p.kind === "compare" && p.columns.length === 2 && (
            <div style={{ display: "flex", fontSize: s * 0.78, fontWeight: 900, height: s * 1.2, alignItems: "center",
              marginBottom: s * 0.3 }}>
              <div style={{ flex: 1, textAlign: "right" }}>
                <span style={{ background: a1, color: "#fff", padding: `0 ${s * 0.35}px` }}>{p.columns[0]}</span></div>
              <div style={{ width: innerW * 0.22 }} />
              <div style={{ flex: 1 }}>
                <span style={{ background: a2, color: "#fff", padding: `0 ${s * 0.35}px` }}>{p.columns[1]}</span></div>
            </div>
          )}
          {body()}
          </div>
        </div>
        {p.source && (
          <div style={{ position: "absolute", left, right: left, bottom, fontFamily: FONT, textAlign: "right",
            fontSize: note, color: pal.muted, whiteSpace: "nowrap", overflow: "hidden",
            textOverflow: "ellipsis" }}>{p.source}</div>
        )}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
