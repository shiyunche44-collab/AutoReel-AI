# aimovice

**一个人加一支 AI 团队的产品解说视频工厂**，本地运行。主做汽车，母婴已接入，其它品类加一份 YAML 即可。

输入一个车型（或者交给团队自己找热点），产出带出处的信息型解说视频：横屏 2–8 分钟，或竖屏 60–180 秒。
做法对标「三驱兄弟」：官方宣传片和实拍只当背景，信息靠叠加层传达，包括参数面板、大字关键词、数据卡、大数字、概念动画和章节条。
成片之后，团队负责发布、抓数据、回评论、做复盘，复盘结论再回灌下一轮选题。

```
        ┌──────────────── 打法手册回灌(选题加权 / 开场结尾 / 标题写法) ────────────────┐
        ▼                                                                              │
 情报员 / 行业日历 / 对标 / 快反 → 主编排选题 → [你:选题闸] → 素材官建池 → 制片出片(机器过门禁 A/B)
                                                                                       │
 复盘官 ← 分析师(1h/24h/72h/7d) ← 运营官(评论) ← 发布员定时发 ← [你:成片闸] ← 合规审核单 + 分发官(小红书图文/切片)
```

### 几条设计原则

- **LLM 负责写，代码负责查。** 脚本里每个数字都必须引用一条事实，并能溯源到原文。十几种 lint 拦截 LLM 的常见毛病：编数字、套话开场、空洞评价、口径冲突、术语写错。统计用确定性代码算，LLM 只写结论文字。
- **不调用视频生成模型。** 花钱的只有 TTS、几次 LLM 调用和每台车一次性的素材打标签，单条成本在几分钱量级。
- **人只把两道闸。** 一道是选题闸，一道是成片发布前的成片闸。中间的门禁 A/B 由机器检查，零 blocker 时自动放行，有问题就停下转人工。
- **失败就降级，不卡住流程。** 概念动画渲不出就退回数据卡，生成的封面质检不过就用老封面，某个事实源挂了就跳过这一源。

---

## 快速开始

### 1. 依赖

| 组件 | 必需? | 说明 |
|---|---|---|
| Python 3.12 | 是 | `pip install -r requirements.txt`；连测试工具一起装用 `pip install -e ".[dev]"`（清单的单一来源是 `pyproject.toml`） |
| ffmpeg | 是 | 本机装在 `C:\ffmpeg\bin`，不在系统 PATH，在 `.env` 的 `FFMPEG_BIN` 里指定 |
| DeepSeek API Key | 是 | 事实抽取、脚本、逐拍版式、团队各角色的文字工作都用它 |
| M Plan 订阅 Key（或 `mcode-tools`） | 是 | 语音、生图、图片理解、网络搜索的通道。见下文「接口后端」 |
| yt-dlp | 是 | 素材池从 B站 / YouTube / TikTok 拉片。YouTube 还需要 PATH 上有 node 或 deno |
| node 22+ 和 HyperFrames | 可选 | 概念动画用。在 `motion/` 下运行 `npm install`。没装时这类拍退回数据卡 |
| playwright | 发布和数据用 | 只装 Python 包，复用本机 Chrome / Edge，**不用** `playwright install` |
| faster-whisper | 可选 | 语音质检的转写复核。没装就只做信号检查 |

### 2. 配置

```bash
copy .env.example .env
```

至少填 `DEEPSEEK_API_KEY` 和 `MINIMAX_PLAN_KEY`，其余都有默认值。常改的几项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEFAULT_CATEGORY` | `auto_long` | 默认品类 |
| `TEAM_ENABLED` | `1` | AI 团队总开关。开着时，服务一启动就会按节奏真实调用 LLM、抓数据 |
| `TEAM_AUTO_GATES` | `1` | 团队出的片零 blocker 时自动过门禁 A/B |
| `TEAM_DAILY_SLOTS` / `TEAM_SLOT_TIMES` | `1` / `18:30` | 每天出几条、几点发 |
| `TEAM_ASPECT` / `TEAM_DURATION` | `16:9` / `240` | 团队选题默认的画幅和时长 |
| `COVER_BRAND` | 空 | 封面右下角的账号署名 |
| `BGM_ENABLED` | `1` | 成片是否配 BGM（混音时对旁白做侧链闪避） |
| `PIPELINE_CONCURRENCY` | `1` | 同时推进几个项目。内存吃紧就保持 1 |
| `YTDLP_PROXY` / `YTDLP_COOKIES` | 空 | 国内访问 YouTube / TikTok 要配代理；B站高清和抖音下载要配 cookies |
| `HYPERFRAMES_BROWSER_PATH` / `MOTION_NODE` | 自动找 | 概念动画用的 Chrome 和 node 22+（nvm 全局是旧版本时手动指定） |
| `PLAN_EXPIRES_ON` | `2026-10-31` | M Plan 到期日，界面上会显示提醒 |

完整清单见 `.env.example`。

### 3. 启动

```bash
powershell -ExecutionPolicy Bypass -File scripts\serve.ps1 -Port 8848
```

打开 http://127.0.0.1:8848 。首页的环境自检要三项全绿（ffmpeg、接口通道、LLM）才能完整跑通。

- 要长期运行就用 `serve.ps1`：它会起一个独立进程，日志写到 `data/logs/server-*.log`。直接运行 `python run.py` 的话，进程会被托管任务的 1 小时上限一起收走。
- `serve.ps1` 会先**杀掉已经占用该端口的进程**，启动前先确认端口上没有正在渲染的服务。
- **只能跑一个服务。** 两个服务连同一个库时，两边的调度器会同时推进同一个项目，结果是重复渲染、成片损坏。开发调试用 `.claude/launch.json` 里的 `aimovice-dev`（端口 8849），并且要另配 `DATA_DIR`。

### 4. 出第一条片

**团队模式（日常用法）**：打开 `/team`。

1. 情报员和主编按节奏跑，也可以在页面上手动触发。跑完后选题单出现在「待我处理」里，批准、驳回或修改都行。
2. 批准后由素材官建素材池，制片到点建项目、出片。机器检查不过的项目会停在「等你处理」。
3. 成片进入成片闸：预览、标题、封面、合规审核单，以及小红书图文和切片都在这张卡上。点通过后按排期定时发布。
4. 之后数据、评论、周报、打法手册都显示在 `/team` 页上。

**手动模式（单条打磨）**：在首页填车型，选主题、画幅和时长，然后创建。项目会自动跑到门禁 A 停下，你改稿、确认后继续渲染，到门禁 B 审片，确认后合成。流程细节见下文「单条视频流水线」。

---

## AI 团队（`app/team/`）

13 个角色共用一个 SQLite 库，由后台调度器按节奏运行（`app/team/scheduler.py`）。每次运行都记一条 `team_runs`，`/team` 页能看到每个角色上次和下次运行的时间，也可以一键暂停。

| 角色 | 节奏 | 做什么 |
|---|---|---|
| 情报员 `scout` | 每天 08:00 / 18:00 | 抓懂车帝销量榜和热度榜、新闻、抖音热搜、B站热门，写入 `trend_snapshots`，并计算车型名次的涨跌 |
| 行业日历 `events` | 每天 08:20 / 18:20 | 从新闻里挑出带日期的上市、发布会、车展、OTA、降价事件。提前 3–7 天交给主编，事件当天交给快反 |
| 对标分析员 `benchmark` | 每天 07:30 | 抓对标账号主页，记录每条作品的数据和爆款倍数（点赞除以该号中位数）。对标账号在 `/team` 上按链接添加 |
| 快反 `rapid` | 每小时 | 车型榜单急升进前 5，或遇到事件当天，就单独出一条竖版 60–90 秒的选题。素材不够时会写明 |
| 主编 `editor` | 每天 09:00 | 给未来几天的空档排选题：去重、冷却、按热度排，并乘上打法手册的权重。结果进**选题闸** |
| 素材官 `asset` | 每 30 分钟 | 选题批准后，提前给这台车建或补素材池 |
| 制片 `producer` | 每 5 分钟 | 选题到开工时间就建项目。门禁 A/B 零 blocker 就替你确认，被拦下就停下等你 |
| 分发官 `repurpose` | 每 10 分钟 | 顺带产出小红书图文笔记（3:4 封面加数据卡加正文）和 1–2 条 30–45 秒竖版切片，跟主片一起审 |
| 发布员 `publisher` | 每 5 分钟 | 成片闸通过后按排期定时发布：图文在主片后 3 小时发，切片在主片后第 k 天发 |
| 分析师 `analyst` | 每 30 分钟 | 从抖音创作者中心把发出去的作品对上号，在发布后 1h、24h、72h、7d 各抓一次播放、完播率、平均观看时长、2 秒跳出率和涨粉 |
| 合规审核官 `compliance` | 每小时 | 发布前出审核单，有红项就不让过闸。发布后 24 小时播放低于账号中位数的 20% 时判为疑似限流，**暂停该账号的自动发布**，需要人工恢复 |
| 运营官 `engagement` | 每 2 小时 | 拉新评论，分成问题、夸奖、抬杠、广告四类并拟好回复，**你批量审核过才发**。问题类评论回灌给主编当选题。断更 3 天会提醒 |
| 复盘官 `coach` | 每周日 20:00 | 按开场、结尾、标题写法、车型、主题等维度统计效果，更新打法手册，出周报和下周的实验建议 |

**闭环是怎么接上的**：复盘官写出 `learnings` 后，`playbook.install()` 把权重挂到三个地方：主编选题、`variety` 选开场和结尾、`packaging` 排标题写法。三处都保留 30% 探索。效果统计对小样本做收缩，同一取值不到 3 条样本时不下结论，效果差异在 15% 以内的也不算数。

**没有打法手册时，行为和不开团队完全一样**：hook 只在 `team.scheduler.start()` 里安装，所以测试和 golden 快照不受影响。

---

## 单条视频流水线（`pipeline/orchestrator.py`）

```
选题 → 事实采集 → 解说脚本 → 逐拍版式 → [门禁 A]
    → 旁白 TTS（决定每拍时长）→ 逐拍渲染 → [门禁 B] → 拼接混音 → output.mp4 + 封面
```

- **画幅在建项目时就定死**（`projects.aspect`，由 `pipeline/aspect.py` 统一管理）。横屏时长可选 120/240/360/480 秒，竖屏可选 60/90/120/180 秒。竖屏是三段式布局：顶部标题带，中间是 16:9 的素材带，底部放数据和字幕，并避开平台安全区。概念动画有真正的竖版模版。
- **成片长度跟着旁白走**：横屏按实测语速 5.5 字/秒估算，竖屏按 4.7 字/秒；稿子超长时，竖屏会再让 LLM 缩一轮。
- **后台调度器每 20 秒推进一次**处于自动态的项目（渲染、合成、QA 通过后）。**不要手动调 `/advance`**，否则会和调度器抢着写同一批文件，把成片写坏。人工只在 GATE_A 和 GATE_B 调 `/gate`，然后轮询 `/status`。
- 同一个项目同一时刻只有一个写入者。人工操作和后台推进撞上时，人工操作会收到 409。不同项目之间可以并行。
- 服务被收走时，渲染在拍与拍之间收手，重启后从断点接着跑。

### 门禁 A：机器先查，人只看判断题

保存脚本时会立刻跑一遍预审，结果直接显示在脚本页上：

- **error（必须改）**：点「确认 A」会被拦下。确实要带着问题继续，就在弹窗里选「仍然继续」。
- **warning（提醒）**：不拦，但值得扫一眼。

脚本保存后会同步写进分镜（`explainer.sync_narration`），因为旁白是按分镜合成的。照抄旁白的大字、引用了已删数字的大字会被丢掉，两行都丢光的拍退回 `broll`。

### 门禁 B 和收尾

- 门禁 B 审已经渲好的各拍，不满意的可以点「换背景重拍」。
- 合成时先单独混出音频 `_render/mix.wav`，再交给主命令。原因是 loudnorm 和视频编码放在同一条命令里时，ffmpeg 会掉 3 秒左右的音频块。出片前会数音频包，发现空洞就不放行。
- 语音质检（`voice_qa.py`）：所有句子做信号检查（哑音、过轻、语速离群），可疑的句子再用 whisper 转写，核对它念的是不是本句。不合格的句子自动重新合成，最多 `VOICE_QA_RETRY_ROUNDS` 轮。

---

## 内容：品类、主题、多样性

### 品类 = 一份 YAML（`app/categories/profiles/`）

| 品类 | 文件 | 静图来源 | 专属事实源 |
|---|---|---|---|
| 汽车 | `auto_long.yaml` | 懂车帝图库（需登录）、汽车之家官图 | 懂车帝在售车型表 `dongchedi.lineup` |
| 母婴 | `baby_long.yaml` | 品牌官网、图片检索 | 企查查召回和抽检记录 |

**新增品类只加 YAML，引擎不改。** 引擎里所有随品类变化的文字都在 `vocab:` 块里，字段说明见 `categories.Vocab`，包括：

- 称呼和举例、素材视角表、细节部件词表
- 术语纠错 `term_guard`、读音 `pronounce`、硬料词 `hero_terms`
- 概念动画场景和取值刻度 `concept_ranges`
- 原理清单 `principles`（`k_` 开头，里面不许写数字）

改了提示词就重新生成 golden 快照：

```bash
python tests/test_profile_vocab.py --regen
```

改完 YAML 要重启服务，因为 `load_profile` 有进程内缓存。

### 主题（`themes`）

| 主题 | 用途 |
|---|---|
| `buyguide` | 单车选购深讲（默认） |
| `deepdive` | 围绕一个角度深挖（必须带角度，≤240 秒） |
| `qa` | 一问一答，每个问题一章 |
| `pk` | 两台车按维度对决 |
| `debate` | 围绕争议事实展开 |
| `roundup` | 3–5 台车横评，每台车一章、各用自己的素材池；时长短时按层级砍段落 |
| `priceband` | 价位段选购：先讲这个价位该看的标准，车只是例子 |

每个主题声明自己的事实来源、所需事实层级、脚本规则和段落块。`plan_for()` 按时长和段落权重确定性地铺出段落序列。

### 不让每条片子都长一个样

- **变体**（`pipeline/variety.py`）：每个项目选一种开场（冲突三段式 / 问题 / 场景冷开 / 破除说法 / 硬数字）和一种结尾（提问或预告 / 二选一 / 下期预告 / 一句判决 / 决策卡），会避开同品类最近 4 条用过的组合。有打法手册时按效果加权。
- **视觉包**（`pipeline/looks.py`）：工程 HUD（默认）、暗场电影、工程蓝图、杂志编排。每个包整套换掉面板、字幕、broll 样式、概念模版和 BGM 曲风。在项目页挑选，样图在 `app/web/static/looks/`。
- **热点选题**（`pipeline/topics.py` + `providers/trends.py`）：4 个热点源各自独立失败、缓存 2 小时。只推荐出现在热点里的车，并附上证据。证据会随项目进入事实采集。

### 脚本规则（`pipeline/script_gen.py`）

- **开头三段式**（默认开场）：0–3 秒抛冲突（第一句不超过 18 字，不许用套话），3–10 秒兑现那个数字，10–15 秒亮出处（全片只说一次）。
- **每个数字后面要跟一句「所以呢」**，并给出立场；结尾必须有提问或预告。
- **一段 = 一拍 = 一个版式**，每拍不超过 28 字，太长就按标点拆开。
- **主论点**（`thesis`）：一句不超过 30 字的判断，和脚本在同一次 LLM 调用里产出。结论段必须原样包含它，它同时也是封面副标题。

---

## 画面

### 版式（`pipeline/explainer.py`）

| 版式 | 画面 |
|---|---|
| `broll` | 全屏宣传片，自动轮换全屏 / 角标 / 分屏样式 |
| `panel` | 宣传片加左侧参数面板 |
| `highlight` | 宣传片缩成小窗，加两行大字关键词 |
| `card` | 整屏数据卡（参数 / 对比 / 柱状 / 时间线），带本地动画 |
| `stat` | 宣传片压暗，中央大数字从 0 滚到目标值，可带一行注释 |
| `concept` | HyperFrames 概念动画，场景包括电池包、补能、加速、尺寸、激光雷达、芯片、对比 |

- 面板、卡片、大数字上的每个数字都会逐行对照事实核对（`cards.ground`）。核不过时先尝试救回（改成面板或单行），救不回才退回 `broll`。
- **节奏硬约束**（`rhythm.py`）：每 5–6 秒必须出现一次信息层变化，`broll` 的拍数占比和秒数占比都超过三成才算超标。体检结果写进 `rhythm.json`。
- 概念动画有下限：`min(6, max(3, round(拍数 × 0.13)))`。不够时把 `panel` / `card` 升级成概念动画，前提是关键词命中场景，配错图宁可不配。
- 每个数字旁边都标口径（如「CLTC · 标准版 | 官方 / 媒体 / 推算」），底部压一行来源。这是本片最难被抄走的东西。

### 概念动画（`motion/`，HyperFrames：HTML + GSAP → mp4）

- `motion/scenes/*.html`：7 个场景专用模板（`studio`，默认），用实车棚拍图做标注，或画 X 光扫描和引线。
- `motion/templates/*.html`：4 套通用模版，分别是 `cinema`、`blueprint`、`racing`、`editorial`，风格参照懂车帝和汽车之家官方动画。已退役的模版 id 会自动回落到 `studio`。
- `_viz.js` 绘制 7 种场景图形，刻度取自 `vocab.concept_ranges`，没有真实比例就不画数量图。每行数据的出现时间对齐到旁白念出那个数字的时刻（`explainer.mark_reveal_times`）。
- 渲染结果按模板、数据、时长和底图缓存在 `data/_cache/motion/`。GPU 挂起时自动切换到软件渲染，也可以设 `MOTION_BROWSER_GPU=off` 强制软件渲染。

### 素材池（`pipeline/library.py`）

**先攒、再切、再看、最后铺**，按车型存在 `data/library/<车型>/`，跨项目复用。

1. **攒**：**官方号优先**。用 B站空间接口按官方账号拉投稿（`official.bilibili_official`），官方源最多 8 个；有 4 个以上官方源时，第三方源限 3 个。再加上手动贴的链接和官方图库。
2. **切**：用 ffmpeg 场景检测，按原片自己的剪辑点切段。
3. **看**：每段抽一帧给视觉模型打标签，标视角、是不是这台车、有没有字幕、水印或主持人。为了省钱，废段率高的源提前停，可用背景攒够就停，近重复的段直接继承标签。
4. **铺**（`library.fill`）：2–4 秒一刀，排序依次是确认本车的干净视频、官图、带字幕的段（裁掉底部）、有主持人的段。第三方视频裁掉四角的水印。开场钩子只用确认本车的素材。近重复簇在最近 6 刀里不复用，不连续两刀同一视角。旁白点名某个部件时，不配其它部件的特写。

```bash
python -m app.pipeline.library build 小米YU7 --brand 小米         # 构建 / 续建（--category baby_long 换品类）
python -m app.pipeline.library add 小米YU7 <链接>...               # 加自动搜不到的素材（抖音只能走这里）
python -m app.pipeline.library add-images <主体> <图片|详情页>... --category baby_long
python -m app.pipeline.library show 小米YU7                        # 统计
python -m app.pipeline.library sheet 小米YU7 [--usable]            # 导出联系表，人工过一遍标签
```

**画面质量的上限是素材池。** 项目页会显示「确认本车」视频的秒数；少于 180 秒时会提示，并提前重新找源。

懂车帝图库从 2026-10 起需要登录：在素材库页点「登录懂车帝」，登录态存在 `data/_browser/dongchedi.json`。

### 封面（`pipeline/cover_art.py`）

全站只有一套版式，方便观众认出是同一个号：浅色影棚底、左上角超大粗黑主标题、强调色带副标题（主论点）、车名胶囊、右下角署名。横屏出图后，自动再补一张 3:4（小红书 / 抖音网格）和一张 4:3。

车的图片有三级来源：

1. **真车抠图（首选）**：用素材池里的白底棚拍官图抠出车身，放到 MiniMax 生成的空影棚底上。影棚底按色调和画幅缓存，所有项目共用。
2. **AI 生图兜底**（`COVER_AI_CAR`）：拿官方图做参照，图生图出一台白底车，过四道质检门（能抠图 / 朝向对 / 画面干净 / 和官方图是同一款），任何一道拿不到结论都判不通过。用了 AI 车的项目，简介里会自动加一句声明。
3. **老模板**（`narration.make_cover`）：只用来保证封面永远不会拦住成片。

标题按像素宽度自适应换行，不会把一个词截成两半。汽车之家图上的车牌 logo 在封面上会被抹掉。

```bash
python -m app.pipeline.cover_art --project p_xxxxxxxx    # 只重出封面
python -m app.pipeline.cover_art --all
```

### 旁白与 BGM（`pipeline/narration.py`）

- TTS 逐句缓存在 `data/_cache/tts/`，门禁 A 改几句再确认时，只为改动的句子付钱。字幕用原稿（阿拉伯数字、kWh），TTS 用规范化后的文本，并按 `vocab.pronounce` 纠正读音。
- BGM 来源：MiniMax 音乐接口对新用户返回 410，所以改为从 Mixkit 自动收集免费可商用的曲子，放在 `data/bgm/<品类>/`，许可证记在 `_licenses.json`。也可以把自己的曲子放进 `BGM_DIR`。`BGM_AUTO_COLLECT=0` 关闭自动收集。

---

## 事实与合规

### 事实（`pipeline/intake.py`）

- 每条事实都带 `scope`（`company` / `product` / `industry`）、`basis`（口径）、`source_kind`（官方 / 媒体 / 推算），以及来源工具、查询串和取数日期。
- **推算事实**（`derive.py`）：马力换算千瓦、充电倍率、百公里电耗等。脚本里引用推算数字时必须写「约」或「粗算」。
- **旧款年份去重**（`mark_stale_years`）：新款改了同一个参数时，旧款的那条事实停用。懂车帝的在售车型表事实不参与停用。
- **竞品对比事实**（`compare_with`）：一条事实里同时写清两台车的同一维度，只能用在对比段。只讲别家车的事实直接丢弃。
- 超过 12 个月的参数必须注明年份，并降为 `low` 置信度。所需层级的事实少于 3 条时，界面会提示补料。

### 合规与质量 lint（`script_gen`）

| 判据 | 级别 |
|---|---|
| 含数字但没有事实引用 / 引用了不存在的事实 | error |
| 品类禁用词、广告法绝对化用语、术语写错（`term_lint`） | error |
| 开场第一句是套话或超过 18 字（只查开场段） | error |
| 空洞评价（「很猛」「闭眼入」）、贬低竞品、拿厂商口径当对比论据 | error |
| 对比事实出现在本车段落、横评里引用了别家车的事实 | error |
| 同一参数出现两个取值（`conflict_lint`）、充电区间没说明（`range_lint`） | warning |
| 推算数字没写「约」、硬料没用上、对比段没点名对手 | warning |
| 数字后面没跟「所以呢」、没有立场、没讲原理、缺少口径 | warning |
| 同一事实或数字反复出现、结论段没回扣主论点、结尾没有行动 | warning |

LLM 偶尔整篇用中文数字写稿来绕过数字 lint，所以 lint 前会先统一转成阿拉伯数字（`grounding.arabicize`）。**仍然拦不住的是 LLM 编出来的定性缺点**（如「天际屏反光」），这类问题要靠成片闸上的人工把关。

---

## 发布与数据

页面：分发台 `/publish`、数据看板 `/analytics`、团队 `/team`。

| 平台 | 发布通路 | 数据来源 |
|---|---|---|
| 抖音 | 创作者中心网页版（浏览器自动化）；有企业资质时可走开放平台 `create_video` | 创作者中心作品列表接口（完播率、2 秒跳出、涨粉等） |
| 小红书 | 浏览器自动化，支持视频和图文笔记 | 后台页面文字解析 |
| B站 | 创作中心 Web 投稿（Cookie） | 公开接口 `archive/stat` |
| YouTube | Data API v3 + OAuth | `videos.list` |
| 视频号 | 浏览器自动化 | 助手页面文字解析 |

- **发布是任务，不是一次函数调用。** 每次发布写一条 `publications` 记录，状态依次是 `pending → uploading → submitted → published`，失败会进 `failed` 并按次数重试。唯一键是（项目, 账号, 画幅, 分件），保证不会重复发；进程重启后任务还在。同一账号的任务严格串行，跨账号可以并行。
- 有 `scheduled_at` 的任务到点才会被领取。账号被合规审核官暂停时，它的任务都不领。
- 文案由 `publish/metadata.py` 生成：按平台字数上限裁标题，不切在词中间；简介带上免责声明和素材来源；发布前会再跑一遍禁用词检查。
- 浏览器登录态存在 `data/_browser/<平台>.json`，在分发台点「登录」扫码一次即可。登录过期时，相关角色会停下并提示你重新登录，不会反复重试。
- 凭据存在 `data/accounts.json`，对外接口只返回 `has_credential` 和末 4 位。应用级密钥放 `.env`。
- **缺失的数据就是 None，不用 0 顶替。** 0 和「没权限」在看板上是两回事。

---

## 接口后端与成本

所有外部能力都走 `providers/cli.call(工具名, 参数)`，由 `CONNECTOR_BACKEND` 决定实际去向：

- **`direct`**：用 M Plan 订阅 Key 直连 MiniMax 开放平台，覆盖配音、音乐、生图、图片理解和新闻检索。图片检索走 Bing。同花顺和企查查不支持，这两个来源会记为失败，其余照常。
- **`mcode`**：`mcode-tools` CLI，只能在 MiniMax Code 里用，可以调用同花顺、企查查和图生图。
- 留空时，有订阅 Key 且找不到 `mcode-tools` 就自动走 direct。自检命令：`python -m tests.verify_direct`。

⚠️ TTS、图片理解和搜索都依赖 `MINIMAX_PLAN_KEY`（套餐接口），**M Plan 到期前必须续费**。

**成本**：界面上的金额是按标价折算的**等效用量**（`costs.py`），不是账单。每个项目有额度上限（`quota_budget`），花钱的步骤在调用前先预留额度，超限时项目转入 `PAUSED_BUDGET`，不算失败。在项目页提高上限后，项目会接着跑。

---

## 目录结构

```
app/
  main.py            FastAPI 应用、页面路由、项目调度器
  config.py          配置（.env）
  models.py / state.py / db.py   领域模型、状态机、SQLite（运行态）+ JSON（可编辑内容）
  costs.py           等效用量估算与额度
  llm/               LLM 抽象（DeepSeek；MiniMax 可选，未验证）
  providers/         外部通道：cli / direct / 素材源 / 热点 trends / 车型表 car_lineup / BGM / 浏览器抓取
  categories/        品类 Profile 加载、段落铺展、校验（profiles/*.yaml）
  pipeline/
    orchestrator.py      流水线编排与门禁
    topics.py            热点选题
    intake.py / derive.py / grounding.py   事实采集、推算、数字与口径工具
    script_gen.py        脚本生成 + 全部 lint
    explainer.py         逐拍版式、核对、时长、渲染、合成
    rhythm.py            节奏体检
    variety.py / looks.py   开场结尾变体、视觉包
    cards.py / overlays.py / overlays_portrait.py / hud.py   本地绘制的叠加层
    motion.py / motion_assets.py / concept_styles.py         概念动画
    library.py / vision_verdict.py / logo_detect.py          素材池
    narration.py / voice_qa.py / render.py / timeline.py     旁白、质检、ffmpeg 合成、字幕
    cover_art.py / packaging.py / repurpose.py               封面、标题包装、一稿多发
    aspect.py            画幅规格（横 / 竖的唯一来源）
  team/              AI 团队：13 个角色 + scheduler + features/stats/playbook（复盘统计）+ views
  publish/           多平台发布：适配器契约、账号、文案、发布任务、浏览器会话、platforms/
  metrics/           数据抓取、快照、聚合
  web/               Jinja2 模板 + 原生 ES Module（零构建）
motion/              HyperFrames 子工程：_shell.html / _lib.js / _viz.js / scenes / templates / regress
scripts/             serve.ps1、motion_regress.py、look_samples.py、reset_to_gate.py 等
tests/               pytest 用例 + verify_*（花钱自检）+ probe_*（网络探针）+ golden/
data/                运行数据（gitignore）：projects / library / _cache / _browser / bgm / logs
```

---

## 测试

```bash
python -m pytest tests/ -q                     # 单元测试，不联网、不花额度
python -m pytest tests/test_full_pipeline.py -q   # 端到端：走真的状态机与 lint
python -m tests.verify_llm                     # 真实 LLM 自检（花额度）
python -m tests.verify_direct                  # direct 后端自检（花少量额度）
python -m tests.verify_number_grounding        # 核对脚本数字是否真在引用的事实里
ruff check app tests
npm run check                                  # 前端：eslint + tsc(checkJs) + vitest
```

- 调用 `orchestrator.create_project` 的测试必须用 `fresh_db` fixture，否则会往真实库里写数据。
- 不要并行跑两个 pytest，它们会互相污染 `data/`。确实要并行，就给每个进程单独的 `DATA_DIR`。
- 发布相关的测试一律不发真稿，走 fake adapter 和样例 JSON。

**概念动画渲染回归**：改了模版或骨架后跑一遍。基线存的是原始像素的哈希。这个回归只能在装了 HyperFrames、Chrome 和中文字体的本机上跑，别放进 CI。

```bash
python scripts/motion_regress.py                  # 和基线逐像素比
python scripts/motion_regress.py --save-png out/  # 顺便存 PNG 肉眼看
python scripts/motion_regress.py --update         # 确认改动后重建基线
```

**检查成片**：

```bash
ffmpeg -v error -i output.mp4 -f null -
```

有输出就说明视频有坏帧。音频空洞要另外用 ffprobe 看包的时间戳，间隔大于 0.05 秒就算空洞。

---

## 已知限制与注意事项

- **闭环还没被真实数据转过。** 打法手册要等每个取值都有 3 条以上带 24 小时数据的作品才会出结论。单账号日更时，需要几周的积累。
- **以下真实路径还没跑过，第一次用时要盯着**：小红书图文上传、评论回复的真实发送、对标账号抓取、多车横评的完整一条。
- **平台接口全是逆向得来的**，包括 B站投稿、抖音创作者中心、小红书和视频号后台，平台一改版就得跟进。解析失败会明确报错并降级，不会假装成功。
- **登录态会过期**，涉及抖音、小红书、懂车帝、YouTube cookies。B站空间接口探测几次就会被限流 30 分钟以上，测试时别反复请求。
- **素材决定画面上限**：新车和小众车的官方素材少。素材池可能混进标错车型或带主持人的片段，建池后先看一眼联系表。
- **Windows 环境的坑**：
  - `python -c` 参数里的中文会乱码，改用 UTF-8 脚本文件。
  - 临时脚本别用标准库模块名命名。
  - 用 curl 访问本机服务时加 `--noproxy '*'`。
- 母婴品类的原理清单和推算规则还没补齐；美妆、数码、家居还没做，可以从 `git show 9b7e42f:app/categories/profiles/` 里的旧草稿起步。
