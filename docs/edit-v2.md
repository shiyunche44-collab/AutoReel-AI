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
  - 实拍拍在分句处下刀(MIN_SHOT 1.2s – MAX_SHOT 2.8s)
  - 每刀把"这一刀念到的半句"交给 pick() 取材 → library.fill(detail=半句)
  - 开头 3 秒钩子:≤1s 一刀 + 大标题,首刀白闪
  - 转场:同拍硬切 / 换章 whip / 进出整屏画面 zoom
  - card / concept / mech 用逐拍成品整段放;取不到素材退回逐拍成品对应片段
  - 叠加层:hook / keyword / stat / panel / chapter;字幕复用 caption_layer
        │  timeline.json(edit_timeline.json 落盘便于复盘)
        ▼
remotion/src/Edit.jsx     竖屏:标题带 + 章节进度 / 素材带(推拉 + 转场 + 叠加层)/ 花字字幕
        │  静音 h264
        ▼
pipeline/edit/render.py   mix.wav(沿用 render._mix_audio,音效在 whip/zoom/flash 处 whoosh)
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

## 已知限制(第 1 阶段)

- 主持人层、片头角标(intro)v2 里暂不叠。
- 对题依赖入库时视觉模型写的描述和视角标签;标错了的素材照样会配错。
- 竖屏的概念动画 / 机构画面仍是逐拍成品,自带旧样式标题带,与 v2 标题带略有差异。
