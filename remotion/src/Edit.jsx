// 剪辑 v2 主合成:读 app/pipeline/edit/edl.py 产出的时间线,一次合成整片(静音视频,混音另走 ffmpeg)。
//
// 两套包装(theme.style,来自视觉包 extra.edit_style):
//   classic     花字关键词 / 侧面板 / 章节小标 / 数据卡 / 关键词冲击推 / 横屏底部章节进度条(edit/Classic.jsx)
//   commercial  对标抖音头部说车号:满屏实拍 + 暗角;左上角粗斜体大字、压暗实拍上的大数字(edit/Headline.jsx),
//               整屏纸面 / 深色图表(edit/Charts.jsx),换章 1 秒章节卡(edit/ChapterCard.jsx),
//               白字无底字幕(edit/Captions.jsx);换章 whip 带水平动态模糊、方向交替;不做冲击推和进度条。
//               调色不在这里:render.stage_media 预裁素材时用 ffmpeg 套 LUT。
//
// 竖屏版面(对标抖音信息类):
//   顶部   标题带(片名) + 章节进度条
//   中部   素材带:实拍按子镜头运镜、转场;叠加层压在这里
//   下部   字幕
//   整屏卡片 / 概念动画 / 片尾卡(kind=beat)直接铺满,不画标题带和素材带
// 横屏:素材带即全屏。
import React from "react";
import { AbsoluteFill, Img, Sequence, useCurrentFrame, useVideoConfig } from "remotion";
import { Cue } from "./Layer.jsx";
import { src } from "./edit/theme.js";
import { BandClip, WHIP, grid, useEmbeddedFont } from "./edit/Media.jsx";
import { CLASSIC_OVERLAYS, ProgressBar, TitleBar, punchScale } from "./edit/Classic.jsx";
import { BigStat, CHook, Headline } from "./edit/Headline.jsx";
import { Chart } from "./edit/Charts.jsx";
import { ChapterCard } from "./edit/ChapterCard.jsx";
import { CleanCue } from "./edit/Captions.jsx";

const COMMERCIAL_OVERLAYS = { hook: CHook, headline: Headline, stat: BigStat, card: Chart, chapter_card: ChapterCard };

// commercial:换章 whip 的方向按次序左右交替;前一刀(紧挨着的)最后几帧跟着甩出去
export const whipPlan = (clips) => {
  let n = 0;
  const out = clips.map((c) => ({ ...c }));
  out.forEach((c, i) => {
    if (c.transition !== "whip") return;
    c.whipDir = n % 2 ? -1 : 1;
    n += 1;
    const prev = out[i - 1];
    if (prev && Math.abs(prev.start + prev.dur - c.start) < 0.05) {
      prev.exit = "whip";
      prev.exitDir = c.whipDir;
    }
  });
  return out;
};

// commercial 暗角:压一点四角,人眼往中间走,也让白字更跳
const Vignette = () => (
  <AbsoluteFill style={{ background:
    "radial-gradient(ellipse 85% 80% at 50% 48%,rgba(0,0,0,0) 55%,rgba(0,0,0,.34) 100%)" }} />
);

// ---------------------------------------------------------------- 整片
export const Edit = (props) => {
  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const now = frame / fps;
  const g = grid(width, height);
  const theme = props.theme || {};
  const commercial = theme.style === "commercial";
  const accent = theme.accent || props.accent || "#FF5A1F";
  const clips = commercial ? whipPlan(props.clips || []) : (props.clips || []);
  const current = clips.find((c) => now >= c.start && now < c.start + c.dur) || clips[clips.length - 1];
  const fullScreen = current && current.kind === "beat";
  const seq = (start, dur) => ({ from: Math.round(start * fps), durationInFrames: Math.max(1, Math.round(dur * fps)) });
  const bandStyle = { position: "absolute", left: 0, width: "100%", top: g.band.top, height: g.band.height, overflow: "hidden",
    boxShadow: "0 0 60px rgba(0,0,0,.55)" };
  const overlays = commercial ? COMMERCIAL_OVERLAYS : CLASSIC_OVERLAYS;
  const box = { w: width, h: g.band.height };
  // commercial:和换章 whip 同时开始的叠加层(章节卡、章节首拍的大数字 / 图表)等素材甩到位再出
  const whips = commercial ? clips.filter((c) => c.transition === "whip").map((c) => c.start) : [];
  const lead = (o) => (whips.some((s) => Math.abs(s - o.start) < 0.08) ? Math.min(WHIP / 2, o.dur / 4) : 0);
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
        <AbsoluteFill style={commercial ? {} : { transform: `scale(${punchScale(props.punches || [], now)})` }}>
          {clips.filter((c) => c.kind !== "beat").map((c, i) => (
            <Sequence key={`c${i}`} {...seq(c.start, c.dur)}>
              <BandClip clip={c} fps={fps} box={box} commercial={commercial} width={width} />
            </Sequence>
          ))}
        </AbsoluteFill>
        {commercial && <Vignette />}
        {(props.overlays || []).map((o, i) => {
          const C = overlays[o.type];
          return C ? (
            <Sequence key={`o${i}`} {...seq(o.start + lead(o), o.dur - lead(o))}>
              <C o={{ ...o, dur: o.dur - lead(o) }} accent={accent} fps={fps} band={{ ...g.band, w: width, land: !g.title }} theme={theme} />
            </Sequence>
          ) : null;
        })}
      </div>
      {clips.filter((c) => c.kind === "beat").map((c, i) => (
        <Sequence key={`b${i}`} {...seq(c.start, c.dur)}>
          <BandClip clip={{ ...c, motion: "none" }} fps={fps} commercial={commercial} width={width} />
        </Sequence>
      ))}
      {!commercial && !g.title && !fullScreen && <ProgressBar chapters={props.chapters || []} duration={props.duration}
        accent={accent} now={now} />}
      <AbsoluteFill>
        {(props.captions || []).filter((c) => now >= c.start && now < c.end).map((c, i) => (theme.subs === "clean"
          ? <CleanCue key={i} cue={c} bottom={g.title ? g.capBottom : 0.075} size={g.title ? 0.075 : 0.065} />
          : <Cue key={i} cue={c} style={{ accent, bottom: g.capBottom, size: g.title ? 0.073 : 0.06,
              box: theme.subs === "outline" ? undefined : theme.subs, boxColor: theme.subBox }} />
        ))}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
