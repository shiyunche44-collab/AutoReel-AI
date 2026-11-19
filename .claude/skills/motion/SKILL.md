---
name: motion
description: 为 AutoReel 的某一拍现写 HyperFrames 概念动画（HTML/CSS/GSAP 片段），用截帧看图迭代到满意为止。适用于分镜里有 concept 拍、用户想要"这里做个动画"，或者库里的模版讲不清这一拍内容的时候。
---

# motion：Claude 现写概念动画

一拍概念动画，就是项目目录下的一个片段 `<项目>/motion/<拍号三位>.html`，再加一个可选的数据文件 `<拍号>.json`。渲染阶段遇到 concept 拍会优先用它。片段过不了 lint，或者渲染失败，这一拍会自动退回数据卡，并记进 `/api/degradations` 的 `concept.to_card`。

## 什么时候值得现写
- 讲的是**看不见、拍不出来**的东西：电流怎么走、热管理怎么换热、悬架怎么动、扭矩怎么分配、充电曲线为什么先快后慢。
- 库里的模版（`motion info` 里的 `library_scenes`）讲不清这一拍。能讲清就直接用库场景，不用现写。
- 第一阶段每部片子现写 **2–4 拍**。写得少而精，比每拍都写强。

## 循环
```bash
python -m app.cli motion info <pid> <beat>              # 旁白、拍长、画幅、数据(__DATA__)、可用事实
python -m app.cli motion new <pid> <beat> [--from motor] # 从库场景复制起点(中文注释已去掉)
# 用 Edit 改片段;中文文案写进 sidecar JSON
python -m app.cli motion lint <pid> <beat>               # error 必须清零
python -m app.cli motion snapshot <pid> <beat> [--at 0.5,3,6]   # 截帧 → 用 Read 看图
python -m app.cli motion render <pid> <beat>             # 整段 mp4 预览(几十秒,可后台跑)
```
这一拍的版式必须是 `concept`：`motion info` 的 `notes` 会提醒。不是 concept 的话，先用 `shots-put` 把 layout 改成 concept，overlay 照分镜规则填 `{"scene": "...", "title": "...", "rows": [...]}`，rows 里的数字取自事实。

## 硬规则（lint 会拦）
- **片段只能有 ASCII**，注释也算。所有中文（标题、标注、单位说明）都写进 `<拍号>.json`，片段里用 `D.xxx` 或 `HF.data.xxx` 读取。原因是 HyperFrames 按页面里出现的字符裁剪内嵌字体，多一个字字体就变了。
- **不能联网**：不能有 http(s) 地址，也不能有 CDN。能用的只有 shell 提供的 gsap、`_lib.js`、`_viz.js`、three.js、工作目录里的底图和车图，以及 `motion/registry/compositions/` 下的组件。
- **画面上的数字都要出自事实**：`__DATA__` 和 sidecar 里出现的每个数字，都必须在某条启用的事实原文里出现过。版式用的数值（`at`、`ratio`、`share`、`seconds`）不算在内。
- **时间轴不能超过拍长**：用 `S`（拍长）和 `T(x)`（按拍长缩放后的时间）排时间，不要写死秒数。
- 竖屏项目要在文件头写 `<!-- @portrait yes -->`，并按竖屏重新排版（见下）。

## 片段结构
片段由三块组成，`motion/_shell.html` 负责外壳：
```html
<!-- @portrait yes -->
<meta name="hf-bg" content="studio" />        <!-- studio 自带影棚底 | footage 压在实拍上 -->
<meta name="hf-photo" content="exterior_side" /> <!-- 要车图就声明视角,不要就删 -->
<style> /* 颜色用 rgba(var(--acc), a) / var(--hi);尺寸按 1920x1080 写 */ </style>
<script>
  const { tl, D, A, T, S } = HF.boot({ portrait: "band", focus: [300, 170, 1700, 950] });
  // ... 画东西、排时间线 ...
  HF.exit(tl, [/* 退场元素 */], A.outAt, A.outDur);
  HF.register(tl);   // 必须:把时间线交给渲染器
</script>
```
- `HF.boot` 返回值：
  - `tl`：gsap 时间线
  - `D`：`__DATA__`
  - `S`：拍长
  - `T(x)`：按节奏缩放后的时长
  - `A.outAt` / `A.outDur`：退场点和退场时长
- `D.rows` 是分镜里核对过的事实行 `[{label, value, values, at}]`，`at` 是旁白念到这一行的时刻。标注出现的时间要跟着 `at` 走（可以用 `HF.coTimes`），这样画面和旁白对得上。
- 常用工具：
  - 元素：`HF.el` / `HF.svg` / `HF.add`
  - 车：`HF.carRig`（车滑入、X 光扫描）
  - 标注和数字：`HF.callout`（引线标注）、`HF.bigNumber` + `HF.countHero`（数字滚动，滚完必须回到原文）
  - 标题和出处：`HF.header` / `HF.footnote`
  - 运动：`HF.flowDots`（沿折线流动的光点，适合电流、冷却液）、`HF.drive`（逐帧驱动）、`HF.camera`（慢推）
  - 收尾：`HF.exit`、`HF.grain`
  - 颜色：`HF.c("acc", 0.5)`，给 SVG 和补间用
  - 竖屏：`HF.portrait`、`HF.safe`（竖屏安全区）
  - 完整实现在 `motion/_lib.js` 和 `motion/_viz.js`，写之前先读一读你要用的那几个函数。
- 范例：结构最简单的是 `motion/scenes/battery.html`；机构类看 `motor`、`damper`、`planetary`、`piston`；能量流看 `powerflow`。

## 看图自审（每次 snapshot 后都要过一遍）
- 0.15、0.5、0.95 三个时刻分别是：开场有没有东西，中段信息完不完整，结尾是不是干净退场。
- 文字有没有溢出或互相压住，数字是不是和事实原文**一字不差**，单位有没有掉。
- 竖屏要看标题区和字幕区（`HF.safe`）有没有被占，右下角的按钮区有没有放字。
- 一屏只讲一件事，最多 3 个标注。旁白讲什么，画面就动什么，别做与旁白无关的炫技。
- 满意以后再 `render` 一次，看整段动效节奏，用 `ffmpeg -v error -i x.mp4 -f null -` 检查有无错误。
- 正式成片里，片段会压在这一拍的实拍或影棚底上（取决于 `hf-bg`）。snapshot 用的是暗色底，颜色对比要留余量。

## 好场景入库
某个片段明显可以复用到别的车上时（参数全从 `__DATA__` 取、没有写死这台车），告诉用户可以抽成库场景放进 `motion/scenes/`。入库要单独做：改成参数化，更新 `motion/regress/baseline.json` 并跑回归，不在出片流程里顺手做。
