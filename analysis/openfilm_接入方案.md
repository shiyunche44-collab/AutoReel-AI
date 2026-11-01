# OpenFilm 接入方案:成片闸「精修」入口

2026-11-01。依据:对 p_c113015a437b 前 7 拍做的可行性实验(scratchpad `of/`),加上对 `explainer.compose`、`render.compose`、`publisher.approve_cut` 的阅读。

## 1. 目标和边界

**要解决的问题**:成片闸现在只有通过 / 打回,看到一个错字或一句不通的字幕也只能整条打回。

**做什么**:成片闸卡片上加「精修」。把成片导出成 OpenFilm 项目,在 Studio 里改,点「应用」后用**我们自己的合成**重出成片。

**不做什么**:
- 不用 OpenFilm 当渲染引擎(`openfilm render` 22 秒要 55 秒,4 worker;字幕样式也和我们的外观包不一致)。
- 不替换概念动画(HyperFrames)和叠加层(PIL / Remotion)。
- 不让 Studio 改旁白音频。

## 2. 关键约束(决定了方案形状)

1. **叠加层已经烧进各拍成片**。`rows[idx].final_path` 是带面板、数据卡、概念动画的完整拍片,叠加层不是独立图层。所以 Studio 里能编辑的只有「整拍」和「字幕」,没法改单个数据卡。
2. **旁白是一整条 wav,按各拍时长对齐**。裁短或删掉一拍会让声画错位,所以 v1 不允许改拍的时长、顺序和删除。
3. **`explainer.compose` 开头会 `rmtree(_render)`**,而且调度器会自动推进 COMPOSING。手动和调度器同时合成会出坏片(2026-10-21 的教训)。应用精修必须走项目锁。
4. **字幕有两条路**:ASS(`narration.ass_path`)或 Remotion 花字层(`looks.sub_layer`,工作区里未提交)。应用时要走和正常成片同一个入口。
5. Studio 只在本机 `127.0.0.1` 可访问,远程控制时点不开。

## 3. 数据流

```
成片出完(plan_item.status = review)
  └─ 点「精修」 → refine.export(project_id)
        film/film.html          viewport = aspect.frame_size
        film/assets/norm_NNN.mp4  各拍成片(硬链接,失败退复制)
        film/assets/mix.wav       旁白+BGM+音效的混音(只做预览)
        film/assets/mix.vtt       由 narration cues 生成
        film/.orig.vtt            导出时的原始字幕,用来比对
  └─ 启动 Studio(固定版本的 openfilm,Node 22+),返回地址
  └─ 人在 Studio 里改字幕
  └─ 点「应用」 → refine.apply(project_id)
        读 film/assets/mix.vtt,和 .orig.vtt 比对
        无改动 → 直接返回
        有改动 → 过字幕检查 → 重建字幕 → 重新合成 → 重跑审核单
```

film.html 里每一拍是一个 `<video>`,按 `at` 首尾相接放在同一条轨,混音是单独一条音频轨。结构和实验里的一致,只是素材从原片段换成了拍成片。

## 4. 新增和改动

**新增 `app/pipeline/refine.py`**(所有 OpenFilm 细节只在这里,其他模块不知道它):
- `export(project_id) -> RefineSession`:生成上面的目录,返回目录和 Studio 地址。
- `studio_url(folder)`:调 CLI 的 `open`,解析输出里的地址。退出码忽略,只看地址(实验里每次都有 libuv 的退出断言)。
- `read_edits(project_id) -> Edits`:读 vtt,逐条和原始比对,返回改了哪几条。
- `apply(project_id)`:见下。

**应用时的检查**(沿用项目原则「LLM 写、代码查」,人工改字也一样查):
- 改后的字幕里出现新数字,必须能在该拍引用的事实里找到(复用 `grounding.has_numeric_claim` / `numbers_in`)。
- 命中 `term_guard` 禁用词、违规词,直接红项。
- 字幕不能和旁白明显对不上:改动字符数超过原句 50% 给黄色警告(旁白是固定的,字幕改太多等于声画不一致)。
- 红项不应用,返回具体哪一句。

**应用后的重合成**:
- 把改后的文字写回 `narration.json` 的 cues,再走和正常成片相同的字幕路径(ASS 或 `_caption_layer`)。
- 直接调 `render.compose`,不走 `explainer.compose`,避免 rmtree 和重做封面、包装。是否能复用 `_render/norm_*` 要实测;不能复用就接受重新归一化的耗时。
- 整个过程持项目锁(`locks.py`),并且只在 plan_item 为 `review` 时允许,`scheduled` 之后不再允许。
- 完成后重跑 `compliance.checklist`,更新成片闸卡片。

**路由和界面**(`app/routes/team.py`、`/team` 页面):
- `POST /api/team/plan/{id}/refine/open` → 返回 Studio 地址。
- `POST /api/team/plan/{id}/refine/apply` → 返回改了几句、检查结果。
- 成片闸卡片加两个按钮:「精修」「应用精修」,以及「已精修 N 句」标记。
- 记录:`preference.record(item, "cut", "refine", ...)`,精修的内容进入「你的审核偏好」的学习材料(你常改哪类字幕)。

**配置**(`config.py`):`OPENFILM_VERSION`(固定,当前是 0.2.7,不用 `@latest`)、`OPENFILM_ENABLED`(默认关)、复用 `config.motion_node()` 找 Node 22+。

## 5. 分阶段做,每一阶段有退出条件

| 阶段 | 内容 | 做完能看到什么 | 退出条件 |
|---|---|---|---|
| P1 | `export` + `studio_url` + `read_edits`,命令行脚本,不碰合成 | 一条真实项目的整片能在 Studio 里打开,改字幕后 `read_edits` 能读出改动 | 整片(108 秒)导出和 Studio 加载正常;vtt 往返不丢句、不乱序 |
| P2 | `apply` + 检查 + 重合成 + 路由 + 卡片按钮 | 在 `/team` 上走完「精修 → 应用 → 审核 → 通过」 | 改一句字幕,成片里对应句变了,其余画面和音频逐字节外的指标不变(时长、音频包数、解码干净) |
| P3 | 补验证项:逐字时间戳标签、Studio 裁剪后 `film.html` 里 `overrides` 的格式、竖屏整片 | 一份验证记录 | 决定要不要做 P4 |
| P4(可选,大) | 导出改用原始素材加透明叠加层,Studio 的裁剪、换素材能映射回 `beats/NNN.json` | 能在 Studio 里换某拍的素材 | 依赖叠加层改成 Remotion 透明层,和已有的 Remotion 计划合并做,不单独立项 |

P1 和 P2 合计估计 1 到 1.5 天。P4 不在本方案范围内,只是留出口。

## 5.1 P1 验证结论(2026-11-01,p_c113015a437b,32 拍 / 34 句 / 108 秒)

- **导出**:`python -m app.pipeline.refine export|open|edits <project_id>` 可用,14 个单测通过。
- **字幕往返通过**:在 Studio 改第 2 句并回车,`edits` 只报 `#2`,句数 34→34,时间轴和 `NOTE` 头原样保留,不丢句、不乱序。
- **整片加载慢但能用**:首次打开要为 32 段 1080p 生成缩略图和代理,约 10 分钟才出齐,之后时间线、字幕行、波形都正常;二次打开应走缓存(未单独测)。内置浏览器里预览画面一直是黑的("正在绘制第一帧"),软件渲染 WebGL 的问题,未查清,所以"在整片上看着改"的体验还没验证。
- **踩过的坑**:出点要按真实文件时长截断(否则 Studio 报 clip 越界);重导出只能删 `film/assets`,删 `.film/` 会让 Studio 返回 410。
- **遥测**:`OPENFILM_TELEMETRY` 不是 OpenFilm 支持的变量(只有 `OPENFILM_NO_BROWSER`、`OPENFILM_RUN`),无需设置。
- **对放弃条件的判断**:vtt 往返没出问题,不触发放弃;"加载明显卡死"一条要看你在真实 Chrome 里的预览是否正常。

## 5.2 P2 实现与实测(2026-11-01)

**做了什么**
- `refine.apply(pid)`:读 vtt 与导出基线比对 → 检查 → 持项目锁 → 改 `narration.json` 的 cue 文字和 ASS → `explainer.recompose` 重出成片 → 更新基线。重出失败会把 narration.json 和 ASS 原样还原。
- 检查沿用包装标题那套(`packaging.build_inputs` 的 `allowed` / `terms`):新数字必须在事实或脚本里出现过(红)、新增禁用词(红)、改成空句(红)、一句改动超过一半(黄,只提醒)。句数变了、项目不在 DONE 一律拒绝。
- `explainer.recompose`(新):和 `compose` 同一套字幕层 / 音效 / `render.compose`,但**不 rmtree `_render`**、不重做封面和包装,各拍归一化缓存直接复用;`render.json` 在原内容上更新。
- `narration.retext_ass`(新):只换原 ASS 里每句的文字,**样式沿用成片当时的**。第一版重生成整份 ASS,实测会套上当前外观包(底色、字色和原片不一致),所以改成原地换字。
- 成片闸:`POST /api/team/plan/{id}/refine/open`、`.../refine/apply`;卡片上「精修字幕」「应用精修」两个按钮;`OPENFILM_ENABLED=1` 才显示,默认关。应用后写一条 `refine` 偏好,进入复盘官的归纳材料。

**真实项目实测(p_c113015a437b,108 秒)**:改一句后应用,**约 65 秒**出新成片;时长 108.5s、视频 2588 帧、音频 5084 包都和原片完全一致,整片解码干净;第 2 句字幕换了、底色和样式不变。测完已把该项目还原。

**没做 / 要注意**
- 应用是同步的:请求要等一次合成(这条片 65 秒,更长的片更久)。如果要在页面上等得更舒服,再改成后台线程加轮询。
- `render.json` 里的 `cover_b`、封面不会重做(字幕改动不影响封面)。
- 预览黑屏的问题(内置浏览器)仍未查清,Studio 里「看着改」的体验要你在真实 Chrome 里试。
- 字幕句数不能增删,拍的时长、顺序、删除都不支持(旁白是一整条 wav)。

## 6. 测试

- `export` 的 `film.html` 做快照测试(固定项目夹具),竖屏和横屏各一份。
- vtt 往返:生成 → 改一句 → `read_edits` 只报这一句。
- `apply` 的检查:新增无出处数字被拦、禁用词被拦、改动过大给警告、无改动直接返回。
- Studio 和 CLI 全部用假进程替身,测试里不联网、不起浏览器。
- 用 `fresh_db` 夹具(创建项目的测试必须这样,否则会污染真实库)。
- 重合成后的体检复用现有的 `_audio_intact` 和解码校验。

## 7. 风险

| 风险 | 缓解 |
|---|---|
| OpenFilm 才 0.2.x,格式会变 | 固定版本;所有细节隔离在 `refine.py`;升级前先跑 P1 的往返测试 |
| 首次运行下载约 100 MB 无头浏览器 | 实验里能下载;失败时「精修」按钮灰掉并说明原因,不影响出片 |
| 只能在本机打开 | 卡片提示「需在这台电脑上操作」;远程时按钮不可用 |
| 精修期间调度器或重新合成改了文件 | 项目锁加状态检查;精修目录是独立副本,不会被 rmtree 影响 |
| 使用遥测 | 我在实验里设了 `OPENFILM_TELEMETRY=0`,但没确认这个变量是否真被支持,P1 要核实 |
| 字幕样式和外观包不一致 | 应用时用我们自己的字幕路径,Studio 里看到的样式只是预览 |

## 8. 放弃条件

P1 如果出现下面任一情况,就不做 P2,改成自己在 `/team` 页面里写一个字幕行编辑器(约 1 天,零依赖):
- Studio 改字幕后写回的 vtt 会改动时间轴、丢句或改格式;
- 108 秒整片在 Studio 里加载或播放明显卡死;
- Studio 在你的机器上不稳定(崩溃、端口冲突)。

**说句实话**:v1 只做字幕精修,价值有限——自己写一个字幕行编辑器也能做到,Studio 的额外价值是整片时间线预览。Studio 真正有优势的是 P4(换素材、改单拍),所以 P1 要看的不只是「能不能跑」,而是「在整片上看着改字幕」是否比文本框明显更好用。这一点要你亲手试了才能判断。
