# 剪辑 v2:统一时间线 + Remotion 主合成

状态:**第 1–3 阶段已落地(试点,默认关闭)**。`EDIT_V2=1` 只对竖屏生效,`EDIT_V2=all` 横竖都走;
任何失败自动退回旧合成,并记一条 `edit_v2:fallback` 事件。

## 为什么重构

审片日志(`docs/team-loop-log.md`)和后台数据里反复出现的问题,与旧方案的结构一一对应:

| 现象 | 旧方案的结构原因 |
|---|---|
| 画面与旁白不符(讲双联屏配车侧面)、串车 | `library.fill` 设计上"背景不要求对上旁白",一拍共用一组背景 |
| 像 PPT、节奏平 | 一拍一版式且整拍烧死;切点不跟着词;拍间基本硬切 |
| 包装土 | 叠加层是 PIL 出的 PNG + ffmpeg overlay,动效只能做很有限的关键帧 |
| 2 秒跳出 30–50% | 开场只是一种版式,没有专门的钩子编排 |
| 难微调 | 没有统一时间线,改一处要重渲整拍 |

## 参考的成熟方案

- **剪映「图文成片」/ CapCut、Opus Clip、Pictory**:按句配画面、关键词花字、1.5–3 秒一刀、转场带音效 —— 目标观感。
- **NarratoAI**(GitHub):视觉模型先看懂素材,LLM 按解说逐句挑片段 —— 第 2 阶段"语义配画面"的思路。
- **MoneyPrinterTurbo**:按句关键词检索素材 + MoviePy 合成 —— 借"每句单独取材",不借合成器。
- **Remotion / Revideo**:React 声明式时间线、逐帧可控 —— 选作主合成器(仓库已在用 Remotion 做花字层)。
- **OpenTimelineIO / editly**:多轨 JSON 描述时间线 —— `edl.py` 的数据形状参照它们。
- **pyJianYingDraft**:生成剪映草稿供人精修 —— 可作为第 4 阶段的人工兜底出口。
- **Chinese-CLIP 等多模态向量 + PySceneDetect**:句子 → 镜头的语义检索 —— 第 2 阶段。

## 架构

```
分镜(ShotSpec, TTS 定好时长) + narration.json(cue + char_t 字级时间)
        │
        ▼
pipeline/edit/edl.py      build_timeline():纯函数
  - 实拍按 `Pacing` 的快慢节奏下刀,目标附近有分句开口就落在开口上;同章同视角的拍连起来下长镜头
  - 每刀把"这一刀念到的半句"交给 pick() 取材 → library.fill(detail=半句)
  - 开头 3 秒钩子:每刀约 1.5–2.5s + 大标题,首刀白闪
  - 转场:同拍硬切 / 换章 whip / 进出整屏画面 zoom
  - card / concept / mech 用逐拍成品整段放;取不到素材退回逐拍成品对应片段
  - 叠加层:hook / keyword / stat / panel / chapter;字幕复用 caption_layer
        │  timeline.json(edit_timeline.json 落盘便于复盘)
        ▼
remotion/src/Edit.jsx     竖屏:标题带 + 章节进度 / 素材带(推拉 + 转场 + 叠加层)/ 花字字幕
        │  静音 h264
        ▼
pipeline/edit/render.py   mix.wav(render._mix_audio:旁白 + 踩点配乐底床 + 三处音效,见「声音」)
                          → -c:v copy 合流 → 解码检查 + 音频包检查 + final_qa
```

流水线状态机不变:DRAFT_RENDERING 照旧逐拍渲染(整屏卡片 / 概念动画要用,门禁 B 也还能看逐拍),
只有 COMPOSING 里的合成换成 v2。封面、包装、render.json 新旧共用(`explainer._finish_compose`)。

### 取材一次定稿(门禁 B 审的就是成片)

开关 `EDIT_V2`:`off`(默认)/ `portrait` / `landscape` / `all`(旧写法 `1` = portrait)。

- **定稿**:逐拍渲染完、进门禁 B 之前,`edit.project.plan` 取材、编时间线,连同每一刀的取材记录
  (`plan.picks`,键是「拍号:第几刀」)写进 `edit_timeline.json`。取材带上近期同车成片用过的
  (`cross_project_seed`)、整屏拍用掉的、排除清单 `exclude_materials.json`。
- **回放**:之后的预览、成片、只改字幕的重合成都按记录回放,不碰素材池。只有这几种情况会重取:
  这一刀的时长 / 半句变了;素材文件没了或进了排除清单;上次没取到、退回了逐拍成品(空记录不回放,
  素材池补上了就换成实拍);重拍某拍(`mark_dirty`,原素材往后排);
  审片「换一刀」(`swap`,这一刀排除原素材)。门禁 A 之后重建分镜会删掉整份时间线(`reset`)。
- **门禁 B 审整片**:进门禁 B 时排 `edit.preview` 任务(`jobs`,资源类 `render` 一次一路),
  半尺寸 + crf 28 + veryfast 出 `preview_v2.mp4`;项目页显示预览和刀表(时间、半句、缩略图、对题),
  每刀可「换一刀」。`EDIT_V2_PREVIEW=0` 关预览。横屏静图秒数占比超过 `GATE_B_STILL_MAX`(默认 0.4)
  门禁 B 拦下,可选「仍然继续」。
- **记录跟着时间线**:有算数的时间线时,`materials_used.json`、来源声明、`features.json` 都按时间线的刀算
  (`explainer.footage.beat_pieces`);成片退回旧合成会把时间线标成 `superseded`,记录退回读逐拍。
- **试片**:`scripts/edit_v2_trial.py` 写 `edit_timeline_trial.json`,不顶掉正式时间线、不进使用记录;
  `--fresh` 全部重取。
- 测试:`tests/test_edit_plan.py`。

## 分阶段

1. **(已完成)时间线 + Remotion 合成 + 按半句取材 + 钩子 + 转场**。`tests/test_edit_edl.py`。
2. **(已完成)语义配画面**(`pipeline/edit/match.py`,`tests/test_edit_match.py`):
   - 视角意图:词表 `vocab.view_words`(说法 → 素材视角),每刀按半句推出想要的视角,
     `library.fill(views=...)` 把命中视角的素材当"对题"排前;
   - 文字模型复选(`EDIT_V2_RERANK`,默认开):`fill` 排序前 6 名的素材描述 + 这半句交给文字模型挑一条
     (`fill(choose=...)`);模型出错 / 编号不合法就按排序取;
   - 对题率:半句点到了视角 / 部件的刀里,素材视角或描述对上的比例,写进 `edit_timeline.json` 的
     `match`,也作为 render.json 的第一条提示(「画面对题 x/y 刀」)。拿它和 cut_review 的
     "画面与旁白不符"一起看趋势;
   - 只改字幕重出成片(`explainer.recompose`)时沿用上次的时间线,同样的取材和刀口,只换字幕。
   - 向量召回(方舟多模态 embedding / Chinese-CLIP)暂缓:Agent Plan 的 Key 是否含向量接口未验证,
     且现有视觉标签 + 文字模型复选已覆盖主要场景。对题率长期上不去再加。
3. **(已完成大部分)包装与节奏**:
   - 数据卡(spec / compare / bars / timeline)改成 Remotion 组件:卡片拍照常取材铺素材带,
     上面压一张玻璃卡,行按旁白时刻(`at`)出现,对比卡左右先后亮,柱状图生长;
     不认识的卡片种类仍用逐拍成品。逐拍渲染仍会出旧卡片(门禁 B 审片用),成片不再用它;
   - 关键词冲击推(`Timeline.punches`):花字关键词念出时素材带放大 7% 再回落,
     至少隔 2.5 秒,钩子段 / 整屏画面 / 数据层期间不推;
   - 视觉包主题(`project.theme_of`):强调色、第二强调色、字幕样式(box 黑底条 / band 色块 /
     outline 描边)跟着 look 走,深色强调色自动提亮到深底上看得清;
   - 竖屏自动重构图(`pipeline/edit/reframe.py`):16:9 素材放进竖屏素材带会裁掉左右两边。
     每刀抽一帧,按列统计边缘能量找主体的水平位置(四边 5% 不算,避开台标 / 黑边),
     裁切窗口和推拉中心都对准它;画面很平或主体铺满全幅时居中。同一帧顺带做模糊底图;
   - 未做:概念动画 / 机构画面迁 Remotion(HyperFrames 模版多、回归基线在本机,单独排期);
     钩子 A/B(要等试点有了对题率和 2 秒跳出数据再设计)。
4. **人工精修出口(可选)**:由 timeline.json 导出剪映草稿。
5. 竖屏数据稳定后,横屏切换并删除旧合成路径。

## 试点怎么验证

**先试已经出过片的项目(推荐)**:不改项目状态、不覆盖原成片,只把合成换成 v2,出一份对比图。

```bash
python -I scripts/edit_v2_trial.py --list          # 已完成的竖屏项目
python -I scripts/edit_v2_trial.py                 # 自动挑最近一条
python -I scripts/edit_v2_trial.py p_xxx --no-rerank
python -I scripts/edit_v2_trial.py --landscape     # 横屏(流水线里要 EDIT_V2=all 才走 v2)
```

横屏版面:素材带即全屏;数据卡靠上、参数面板左侧居中,底部约 20% 留给字幕;
底边一条章节进度条(整屏的逐拍成品期间不画,它们自己烧了章节条)。
所有大字(钩子 / 关键词 / 大数字)按估算宽度自动缩字号,长句不出屏;两行时第二行永远比第一行小。

产物在项目目录:`output_v2_trial.mp4`(新片)、`_edit/compare.jpg`(上排旧片、下排新片)、
`edit_timeline.json`(每一刀念到的半句、取的素材、对没对上题)。

**再在流水线里开**:

1. 本机 `remotion/` 下 `npm ci`;`.env` 加 `EDIT_V2=1`;用 `scripts/restart_server.ps1 -Reason "开剪辑v2试点"` 重启。
2. 找一个竖屏项目在门禁 B 通过,等 COMPOSING 自动出片;`render.json` 的 warnings 第一条是「剪辑 v2 合成」。
3. 失败时看项目事件 `edit_v2:fallback`(已退回旧成片)。`edit_timeline.json` 里每刀的 `text` 就是它取材用的半句。
4. 渲染时长:Remotion 比 ffmpeg 慢。4 核云容器实测 20 秒成片约 70 秒(竖屏模糊底图改成预先抽帧
   模糊的小图后,比实时 CSS 模糊快约 2.4 倍),本机核数多会更快。`EDIT_V2_CONCURRENCY`(默认 `50%`)控制并发,`EDIT_V2_TIMEOUT` 默认 3600 秒。

## 画质与字体

- 输出:h264,CRF 16(`EDIT_V2_CRF`),x264 `slow`(`EDIT_V2_X264_PRESET`),**标准范围 yuv420p + bt709**。
  Remotion 默认出的 yuvj420p(全范围)在平台转码时常被当成标准范围,画面发灰或暗部死黑。
  抽帧 JPEG 质量 95(默认 80 时字边有压缩噪点)。
- 字体:`python scripts/fetch_fonts.py` 把 Noto Sans SC Black(SIL OFL,可商用)下到
  `data/fonts/NotoSansSC-Black.otf`,默认就用它;也可在 `.env` 配 `EDIT_V2_FONT_FILE=<字体文件>`
  (ttf / otf / woff / woff2;.ttc 浏览器认不了)。合成时嵌入,等加载完才出帧,字幕(`Layer.jsx` 的 `Cue`)
  和大字都用它。两样都没有时按「Noto Sans SC Black → 思源黑体 Heavy → 微软雅黑」找系统字体,
  观感随机器变化;成片会带一条提示并记降级 `edit_v2.font`,`/api/health` 的 `edit_v2.font_missing` 为真。
- 渲染缓存键含 `remotion/src` 源码哈希,改了组件旧缓存自动作废。

## 商业包装(视觉包 commercial)

对标抖音头部说车号(三驱兄弟、车圈三七):满屏调色实拍,关键数字和判断上大字,数据段用整屏图表,
不出 HUD / 侧面板 / 章节进度条 / 主持人。视觉包 `commercial`(`looks.py`,`extra.edit_style`)
选中时走这套;剪辑 v2 默认开之前它不参与自动挑选,试片用 `scripts/edit_v2_trial.py --look commercial`。
旧合成(EDIT_V2 关)下它近似 lite 去掉主持人。

**时间线**(`edl.build_timeline(style="commercial")`,纯函数,`tests/test_edit_edl.py`):

- 叠加层只有五种:`hook`(开场大标题)、`headline`(左上角粗斜体大字)、`stat`(压暗实拍上的大数字)、
  `card`(整屏图表)、`chapter_card`(换章约 1 秒的章节卡)。不做关键词冲击推(`punches` 为空)。
- 大字按语义挑,不靠正则:来源是分镜里 highlight / stat / panel 拍已核对过的数据行和判断句
  (`commercial_overlays`)。panel 拍够 3 行(`CHART_MIN_ROWS`)转整屏图表,不够就挑一个带数字的行出大字。
- 限流(`space_headlines`):大字和前后的大字、钩子、大数字至少隔 `HEADLINE_GAP`(6 秒),
  压在图表上的不要,章节卡没走就等它走;章节第一拍就是大数字 / 图表时不出章节卡(两层会叠在一起)。
- 运镜只有缓动的 `drift`(≤3%)和 `push_slow`;转场默认硬切,换章 whip,首刀白闪。

**组件**(`remotion/src/edit/`,`Edit.jsx` 只做编排):

| 文件 | 内容 |
|---|---|
| `theme.js` | 字体、缓动、描边、数字拆分、滚数、弹入等共用函数 |
| `Media.jsx` | 素材带、运镜、转场。commercial 的 whip 带水平动态模糊(SVG `feGaussianBlur` 只模糊 x 方向),方向按次序左右交替,前一刀最后几帧跟着甩出 |
| `Headline.jsx` | `CHook` 开场砸字、`Headline` 左上角大字(数字用强调色)、`BigStat` 大数字从 0 滚到位 |
| `Charts.jsx` | 整屏图表 spec / compare / bars / timeline。纸纹浅底(`chart_bg: paper`)或深色底,同列字号统一,行按旁白时刻 `at` 出现,底部一行数据来源与口径 |
| `ChapterCard.jsx` | 实拍模糊压暗,章节号 `NN / TT` + 强调色细线 + 章名 |
| `Captions.jsx` | 白字无底字幕,横屏单行 ≤16 字(`project.CLEAN_CAPTION_CHARS`);超长句对半切时不切断数字 / 英文代号(`caption_layer._cut_at`,"2.5K" 不会拆成两条) |
| `Classic.jsx` | 旧包装(花字、侧面板、章节小标、进度条),classic 原样不动 |

和换章 whip 同一时刻开始的叠加层(章节卡、章节首拍的大数字 / 图表)由 `Edit.jsx` 往后挪半个 whip,
等素材甩到位再出,不和动态模糊叠在一起。强调色:实拍上用视觉包的亮黄,纸面图表用 `chart_accent`(朱红)。

**调色**(`pipeline/edit/grade.py`,`tests/test_edit_grade.py`):CSS 套不了 LUT,所以在
`render.stage_media` 里用 ffmpeg 预处理。

- LUT 运行时按参数算(轻 S 曲线、暗部偏青亮部偏橙、按饱和度反比加鲜艳度、黑位微抬白位微压),
  33³,缓存在 `data/_cache/luts/<名>_v<版本>.cube`,没有第三方文件。改公式升 `GRADE_VERSION`。
- 每刀实拍按入点和时长预裁(多留 0.25 秒),只缩不放(比画幅大 1.25 倍以上的才缩),统一帧率,
  16 位 RGB 套 LUT 后回 bt709 标准范围,crf 14(`EDIT_V2_GRADE_CRF`)、3 路并行(`EDIT_V2_GRADE_JOBS`);
  静图同一个 LUT 出 JPEG。整屏成品(卡片 / 动画)不调。没带色彩标记的素材按 bt709 解。
- 中间文件在 `_edit/public/g_*`,按内容哈希复用(门禁 B 换刀只重做换掉的那刀),成片合成完删掉
  (一条 6 分钟片约 1 GB)。
- 某刀失败就用原色原入点,LUT 生成失败整片原色;都记降级 `edit_v2.grade`,不拦出片。
- 实测:恒等 LUT 预裁前后平均每通道误差 <1/255(没有色彩矩阵偏移);commercial 平均改动约 2.5–5.5/255。

## 声音(配乐踩点 + 音效)

对标说车号是"整片垫一首有律动的电子乐、换章甩镜带一声 whoosh"。11-12 关掉的配乐 / 音效在
v2 里改成**跟着视觉包开**:`Look.bgm` / `Look.sfx`(commercial 都开,旧包不开配乐),
`.env` 的 `BGM_ENABLED` / `SFX_ENABLED` 写了 1 / 0 就强制开 / 关,不写(或空)才跟视觉包
(`config.bgm_on` / `sfx_on`)。

**曲库与节拍**:曲子在 `data/bgm/<品类>/`(含视觉包子目录、`pool/` 情绪曲库,Mixkit,
许可证和情绪标签记在各目录的 `_licenses.json`)。`pipeline/beat_track.py` 纯 numpy 离线算
BPM、拍点、强拍(小节线)、起势点(drop)、律动清晰度 `pulse`,写回 `_licenses.json`
(`python -m app.pipeline.beat_track`,新曲入库后跑一次)。

**配乐编辑**(`edit/music.py` 的 `plan`,`edit/project.score` 在定稿时调用,结果存进时间线的 `music`):

- 整片一种情绪:按视觉包 `bgm_moods` 的先后取第一个"律动清楚的曲子 ≥3 首"的情绪
  (`project.film_mood`;commercial 是 科技 → 热血),同情绪里按项目 id 轮换,不同片子用不同的曲。
- 起始偏移让曲子的 drop 落在钩子收尾那一刀(`edl.hook_cut`,3 秒附近的切点)。
- 片子比曲子长时在换章处交叉淡入下一首(2 秒),不中途循环;片尾在小节线收,2.5 秒淡出。
- 所有切点(含换章 whip)吸到 ±0.15 秒内最近的拍点(`edl.snap_cuts`):前一刀伸缩、后一刀入点跟着挪,
  同时起的章节卡一起挪。入点前没素材可补、前一刀是整屏成品(卡片 / 动画)或逐拍成品切片、
  挪完不够最短刀长的不动。
- 各段按整曲响度对齐到 -20 LUFS 再叠成底床 `_render/bgm_bed.wav`(旁边 `.plan.json`),
  混音时有这个旁注就不再循环 / 淡变。曲子没打节拍时只按淡入淡出排(`mode: simple`,不吸拍);
  曲库空记降级 `bgm.no_tracks`,底床渲失败,两种都退回旧的按哈希选一首(`narration.json` 的 `bgm_path`)。

**音效**(`pipeline/sfx.py`,CC0 采样在 `data/sfx/pack/`):只在三处响 ——
开场(钩子收尾那刀,前面够 0.8 秒用 riser,否则 hit)、换章 whip(whoosh)、大字 / 大数字落定
(hit,大字弹入后 0.1 秒、大数字滚到位 0.8 秒)。相邻至少隔 4 秒,冲突时 开场 > 换章 > 大字。

**混音**(`render._mix_audio`,单独出 `mix.wav`,loudnorm 不进视频命令):旁白静态增益到 -16 LUFS,
底床比旁白低 10 LU(`BGM_UNDER_LU`),旁白做侧链压底床(阈值 0.05、3:1、起 20 ms、放 350 ms),
混好线性增益到 -14 LUFS、-2 dBFS 限幅。

**实测**(p_32fca54820eb 7:05,只出声音的试混,`docs/benchmark-baseline.md` 的对标口径):

| 指标 | 旧片(无配乐) | 对标讲解片 | v2 |
|---|---|---|---|
| 停顿处底噪比旁白低(`quiet_gap_db`) | 61–64 | 17–36(中位约 21) | 32.2 |
| 有旁白时旁白高出音乐 | — | — | 16 dB |
| 整体响度 | — | — | -14.2 LUFS / TP -1.9 |

底床压低 8 LU 试过:停顿 30.9 dB(只近 1.3 dB),旁白高出音乐从 16 降到 14 dB。11-12 吃过
配乐闪避淹没解说的亏,得不偿失,维持 10 LU。
`music_bed` 的"有没有垫乐"判定对鼓点重的 techno / 摇滚不灵(停顿处频谱太平,像噪声),
这条片子 57% 的窗口判垫乐、结论为"说不清";这是判定器的局限,不是没垫上。

## 节奏与配音

旧的切法是 `MIN_SHOT` / `MAX_SHOT` 夹着匀速切：正片每刀 p10 1.3 / 中位 2.0 / p90 3.0,
长镜头被卡在 3 秒，短镜头几乎没有。对标讲解片(`edit/targets.py`)是快慢交替:
p10 0.75 / 中位 1.7 / p90 3.5,19–32% 的镜头短于 1.2 秒。

**下刀**(`edl.Pacing` + `edl.rhythm_cuts`):

- 正片按 `Pacing.rhythm` 的 10 刀循环(1.8 / 0.8 / 0.7 / 2.6 / 1.6 / 4.2 / 0.9 / 1.5 / 2.2 / 3.8)
  下刀，全片接着数，不按拍重来。每刀落在离目标 ±35%(至少 0.2 秒)内最近的分句开口上，
  没有开口就切在句中。剩下不到「目标 + 1.2 秒」的，整段给最后一刀，不留拍尾碎刀。
- 钩子段(前 `HOOK_SECONDS` 秒)单独切，每刀约 1.5–2.5 秒(`hook_shot`)。计划里写的
  0.6–1.2 秒在对标片里找不到依据：最快的一条钩子段每刀也有 1.43 秒。
  钩子只剩一小截落在某拍时不单切;钩子后面只剩一小截时并进钩子。
- 换章第一刀至少 2.2 秒(`chapter_open`,章节卡压在上面);压着整屏数据(大数字 / 图表)
  的拍，下面每刀至少 2.0 秒(`calm`)。
- 静图一刀最长 2.5 秒(`still_max`),更长的均分，后半段另取一刀，不随 pace 伸缩。
- 同章、同视角(`view` 都标了且相同)、都不压整屏数据的相邻拍连成一段下刀(`_joins`),
  长镜头可以跨过拍界。横屏一拍中位只有 2.6–3.0 秒，按拍切 p90 到不了 3.5。
  跨拍那刀取不到实拍时退回逐拍成品切片(`via="fallback"`)。
- 取材回放按 `拍号:刀序` 匹配，秒数和半句都要一样才复用，所以改了节奏的旧时间线会重新取材。

**pace 旋钮**:`edit.project.pacing()` 按 `knobs.piece_seconds()` ÷ mid 的**平方根**整体伸缩
(fast / slow ×0.82 / ×1.15)。真实分镜试排每分钟 fast 约 36 刀、mid 31、slow 28,
都在对标讲解片 19–36 刀之内;直接乘比值的话 fast 会到 44 刀。(之前这个旋钮在 v2 里不起作用。)

**吸拍**:有配乐时所有切点都吸到 ±0.15 秒内的拍点(`edl.snap_cuts`,见上一节)。

**配音**:

- `TTS_SPEED` 默认 1.10 → 1.0(`.env` 里写了的以 `.env` 为准)。62 条横屏成片(1.08)按
  语速旋钮实测：calm 中位 6.2、mid 6.37、brisk 7.08 字/秒(fit 口径：去空白字数 ÷ 旁白时长)。
  对标讲解片换到这个口径是 6.0(`targets.TARGET_CPS_FIT`)。豆包 `speech_rate` 非线性
  (1.3 只快 17%),降到 1.0 后 mid 约 6.1–6.2。
- `LANDSCAPE_CHARS_PER_SECOND` 5.7 → 6.0。原来的 5.7 比实测慢一成，成片普遍比目标时长短。
  `costs.py` 的 5.8 估算不动。
- 横屏一拍字数上限 `SHOOT_CHARS_MAX` 仍是 28,没按计划降到 22:v2 在拍内按节奏下刀，
  不再一拍一画面，横屏一拍中位已只有 2.6–3.0 秒，拆得更短只会多出拍尾碎刀、少了长镜头。
- 气口不动，只限长停顿(旧规则不变)。

**试渲**(2026-11-19,p_32fca54820eb 7:07,横屏 commercial,`scripts/edit_v2_trial.py`;
旁白沿用原片的 1.08 语速，配音改动没在这条片上体现):

| 指标 | 旧片 | 对标讲解片 | 阶段 5 |
|---|---|---|---|
| 正片每刀 p10 / 中位 / p90(秒) | 1.26 / 2.08 / 3.00 | 0.75 / 1.7 / 3.5 | 0.80 / 1.63 / 2.27 |
| 正片短于 1.2 秒的刀 | 1–8% | 19–32% | 20.5% |
| 钩子段每刀(时间线) | 1.80 | 1.4–3.1 | 2.32 / 1.88 / 1.66 |
| 每分钟切点(场景检测) | 28.1–28.5 | 19.1–35.6 | 36.8 |
| 吸到拍点的切点 | — | — | 138 / 265 |
| 动态占比 / 静帧段占比 | 0.72–0.77 / 0.16–0.20 | ≥0.6 / ≤0.35 | 0.64 / 0.14 |
| 响度 / 真峰值 / 停顿底噪差 | -14.0 / -1.1 / 61–64 | -14 / ≤-1 / ≤40 | -14.2 / -1.5 / 32.4 |

这条片的两台车素材池里没有 1080p 实拍(cookies 体检 B 站仍只给 480p),266 刀里 260 刀是静图。
静图一刀封顶 2.5 秒，节奏循环里 4.2 / 3.8 / 2.6 秒的长刀都被对半切开，所以 p90 只有 2.27、
每分钟切点略高于对标上限。长镜头要等素材池有动态实拍(阶段 1 的重拉);
假取材(1/5 静图)试排三条片，正片 p90 是 3.2–3.6、每分钟约 31 刀。

## 成片验收(对标指标进流水线)

出片收尾(`explainer._finish_compose`)和重合成(`recompose`,精修走的那条)都会用
`teardown.style_metrics` 把成片量一遍，和对标基线(`edit/targets.py`)逐条比，结果写进
render.json 的 `style`(`edit/style_check.py`)。**偏离只出提示，不拦片子**:

- 静图占比由门禁 B 的 blocker 拦(阶段 1)。
- 画面上的问题由成片机审(`cut_review`)管。
- 提示也不进 render.json 顶层的 `warnings`,所以不弹 toast。

| 项 | 对标范围 | 比哪些片 |
|---|---|---|
| 开场每刀(中位) | 1.4~3.1 秒 | 横屏 |
| 正片每刀 p10 / 中位 / p90 | 0.4~1 / 1.6~2.1 / 2.7~4 秒 | 横屏 |
| 每分钟切点 | 19~36 | 横屏 |
| 语速(字幕字数 ÷ 首句起到末句止) | 5.7~6.5 字/秒 | 横屏 |
| 动态画面占比 / 静帧段占比 | ≥0.6 / ≤0.35 | 全部 |
| 整片响度 / 真峰值 | -15~-13 LUFS / ≤-1 dBTP | 全部 |
| 停顿处比旁白低(音乐底) | ≤40 dB | 视觉包配乐时 |

横屏这几项的范围取讲解 4 条的最小值到最大值(出处见 targets.py 和 benchmark-baseline.md)。
对标片全是横屏，竖屏只比通用项。字幕字数和脚本 `fit.spoken_chars` 逐字相等，所以语速和
`LANDSCAPE_CHARS_PER_SECOND` 同一个口径。

- **代价**:一条 7 分钟 1080p 成片量一遍约 35 秒(场景检测和帧差各解一遍码)。
  在合成任务里同步做;`STYLE_CHECK=0` 关掉(这时写 `style: null`,把旧结果盖掉)。
- **查看**:项目页「成片」面板列出每一项，偏离的标黄，并附一句原因。
- **交给终审**:团队成片机审(`cut_review.review_project`)把偏离项以「风格:」前缀并进报告的
  `notes`。只在 `style.video` 就是现在这个成片文件、且量完之后没再重出时才带。

**实测**(p_32fca54820eb):

- 阶段 5 样片报 2 项:
  - 正片 p90 2.27(缺长镜头);
  - 每分钟 36.8 刀。

  两项都是全静图造成的。
- 旧片报 1 项：正片 p10 1.26(缺快切)。

**机审的美感项**(`cut_review`):

- 联系表提示词加了第 5 问「画面好不好看」,新增四种问题:
  - `blurry`:糊、低清放大;
  - `cropped`:车被切掉一大块;
  - `text_on_subject`:字压车身;
  - `empty_shot`:空镜。
- 四种都**只提示**:不复核、不打回、不进 `bad_ranges`。
- 同一张联系表同一种只留头一条，免得一段低清素材刷屏。
- 背景虚化、运动模糊、有意的局部特写、压在背景上的字，都在系统提示里说明了不算。

## 已知限制(第 1 阶段)

- 主持人层、片头角标(intro)v2 里暂不叠。
- 对题依赖入库时视觉模型写的描述和视角标签;标错了的素材照样会配错。
- 竖屏的概念动画 / 机构画面仍是逐拍成品,自带旧样式标题带,与 v2 标题带略有差异。
