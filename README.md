# aimovice

**本地运行的汽车解说视频工厂**。输入一个车型,自动产出 16:9 横屏、2–8 分钟的信息型解说长视频,
对标「三驱兄弟」式的做法:官方宣传片 / 实拍只当背景,信息靠叠加层(参数面板、大字关键词、
数据卡、大数字、章节条)传达。

```
选题 → 事实采集 → 解说脚本 → 逐拍版式 → [人工门禁 A]
    → 旁白 TTS(定每拍时长) → 逐拍渲染 → [人工门禁 B] → 拼接混音 → 成片导出
```

不调视频生成模型:花钱的只有 TTS、BGM、几次 LLM,以及每台车一次性的素材池打标签。

> 竖屏(9:16)的 H3 逐镜头生成链路已于 2026-10-20 删除,需要时可从 git 历史找回
> (删除前最后一个包含它的提交是 `9b7e42f`)。

---

## 快速开始

### 1. 依赖

| 组件 | 状态 | 说明 |
|---|---|---|
| Python 3.12 | 必需 | |
| ffmpeg | **必需** | 已安装于 `C:\ffmpeg\bin`,不在系统 PATH,在 `.env` 里指定 |
| M Plan 订阅 Key **或** `mcode-tools` CLI | 二选一 | 语音、音乐、图片理解、网络搜索的通道。订阅 Key 填 `.env` 的 `MINIMAX_PLAN_KEY`,任何终端可跑;`mcode-tools` 只能在 MiniMax Code 里跑 |
| fastapi / uvicorn / pydantic / httpx / Pillow / PyYAML / python-dotenv | 已预装 | |
| jinja2 / pytest / yt-dlp | 需补装 | `pip install -r requirements.txt`。yt-dlp 给素材池从 B站 / YouTube / TikTok 拉素材,YouTube 还需要 PATH 上有 node 或 deno |

### 2. 配置

```bash
copy .env.example .env
```

编辑 `.env`,**至少填入 `DEEPSEEK_API_KEY`**(事实抽取、脚本与逐拍版式依赖它)。其余项均有合理默认值。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEDIA_PLATFORMS` | `bilibili,youtube,tiktok` | 素材池找源的平台及优先级 |
| `YTDLP_PROXY` | 空 | yt-dlp 代理,国内访问 YouTube / TikTok 需要,如 `http://127.0.0.1:7890` |
| `YTDLP_COOKIES` | 空 | 浏览器导出的 `cookies.txt`。B站高清 / 抖音下载需要 |
| `USE_LIBRARY` | `1` | 是否启用素材池(背景画面全部来自它) |
| `LIBRARY_BUILD_BUDGET` | `1800` | 素材池一次构建的时长预算(秒),`0` 不限 |

### 3. 启动

```bash
python run.py            # http://127.0.0.1:8848
```

**长时间运行请用脱离托管的启动方式:**

```bash
powershell -ExecutionPolicy Bypass -File scripts\serve.ps1 -Port 8848
```

直接 `python run.py` 时,进程是托管后台任务的子进程,托管任务有 1 小时上限,到点会把服务一起收走。
`serve.ps1` 用 `Start-Process` 起独立进程,输出写到 `data/logs/server-*.log`。
(`serve.ps1` 刻意只用 ASCII:Windows PowerShell 5.1 按 ANSI 读无 BOM 的脚本,中文会把解析搞挂。)

首页会显示环境自检:ffmpeg、接口通道、LLM 三项全绿才可完整跑通。

### 4. 跑第一条视频

1. 首页填车型(如 `小米YU7`)→ 选时长(目标值,成片跟着旁白走)→ 创建。
2. 自动跑完**事实采集 → 脚本 → 逐拍版式**,停在**门禁 A**;同时后台开始建这台车的素材池。
3. 检查事实、脚本、分镜,可直接编辑。
4. 确认后先合成全片旁白,每拍时长 = 它那几句的实际时长,然后逐拍渲染。
5. **门禁 B** 审渲好的各拍,不满意的点「换背景重拍」。
6. 确认后直接拼接、混入旁白与 BGM、烧字幕,产出 `output.mp4` 与 `cover.jpg`。

---

## 画面怎么做

### 版式(`pipeline/explainer.py`)

一拍 = 一段旁白 = 一个版式:

| 版式 | 画面 |
|---|---|
| `broll` | 全屏这台车的宣传片 |
| `panel` | 宣传片 + 左侧参数面板(车名、3–5 项参数) |
| `highlight` | 宣传片缩成小窗 + 两行大字关键词 |
| `card` | 整屏数据卡(spec / compare / bars / timeline),柱子生长、左右两列交替亮起等本地动画 |
| `stat` | 宣传片压暗 + 中央一个大数字从 0 滚到目标值 |

- 面板、卡片、大数字的每个数字都逐行核对事实(`cards.ground`),核不过就退回 `broll`;
  大字里的数字、英文型号必须在旁白或事实里出现过。
- 节奏约束:不连续两拍 `stat`,全片大约四成是 `broll`(`enforce_rhythm`)。
- 底部章节条常驻(按段落的 `chapter` 分章,当前章高亮),直接烧进每一拍。
- 卡片与大数字都是 PIL 关键帧 + ffmpeg concat 渲染,不依赖任何生成模型。

### 背景取材(`library.fill`)

从车型素材池挑干净片段连拼,2–4 秒一刀,不要求画面对上旁白。排序:干净的视频 → 官图 →
带烧录字幕的段(裁掉底部 22% 后放大铺满)→ 用过的 → 有主持人的。跳过黑场和源片开头的淡入。

### 素材池(`pipeline/library.py`)

**先攒、再切、再看、最后铺**:

1. **攒**:按车型自动发现源(品牌官方号、宣传片、实拍;B站 + YouTube + TikTok 官方号,口播类不要),
   加上手动贴的链接和汽车之家官图;
2. **切**:ffmpeg 场景检测,在原片自己的剪辑点切段;
3. **看**:每段抽中间帧问一次视觉模型,LLM 转成标签(视角、是不是这台车、字幕 / 海报等问题、一句话描述)。
   省钱措施:每个源先看 8 段,废段率 ≥ 70% 就跳过这个源的其余段;可用背景攒够 240 秒就停;
   同源里画面几乎一样(dHash 距离 ≤ 6)的段直接继承标签。

池子按车型存在 `data/library/<车型>/`,**跨项目复用**。构建是增量的、有时长预算,超时停在当前进度,下次接着做。

```bash
python -m app.pipeline.library build 小米YU7 --brand 小米      # 手动构建 / 续建
python -m app.pipeline.library add 小米YU7 <链接> [<链接>...]   # 加自动搜不到的好素材(抖音只能走这里)
python -m app.pipeline.library show 小米YU7                      # 统计:源、段、各视角可用数、视觉调用次数
python -m app.pipeline.library sheet 小米YU7 [--usable]          # 导出联系表,人工过一遍标签
```

画面质量的上限是素材池:标错车型的片段会被当背景用,建池后先看一眼联系表。

### 旁白与 BGM(`pipeline/narration.py`)

- TTS 逐句缓存在 `data/_cache/tts/`(按模型、音色、语速、文本取 key):门禁 A 改几句稿子再确认,
  只为改动的句子付钱。
- BGM 按 `music_prompt` 建共享曲库(`data/_cache/bgm/`,每个 prompt 最多 3 首),按项目轮换;
  生成失败时退回库里任意一首。

---

## 事实与合规

### 事实可追溯与分层

每个数字型陈述必须携带 `fact_refs`,否则被合规 lint 直接拦截。每条事实都带来源工具、查询串与取数日期。

| 层级(`Fact.scope`) | 含义 | 典型内容 |
|------|------|----------|
| `company` | 公司工商与财务 | 注册资本、法定代表人、证券代码 |
| `product` | **产品参数** | 续航、加速、售价、配置、电耗 |
| `industry` | 行业与市场 | 渗透率、销量排行、政策 |

- iFinD 与企查查只覆盖公司层面,车型参数来自 `ifind.product_news` 的新闻语义检索;产品向查询串要单独一条,
  混在财经查询里会被拉回公司新闻。
- 新闻有时间陷阱:超过 12 个月的参数必须注明年份并降为 `low` 置信度。
- 方向所需层级的事实少于 3 条时,界面横幅提示 `product_gap`,请在门禁 A 手动补充。

### 合规检查(`script_gen.compliance_lint`)

- 品类禁用词 + 方向级附加禁用词 + 广告法绝对化用语
- 引用了不存在的事实 ID
- 品类必需免责声明自动追加
- **禁止贬低竞品**:点名了竞品 / 友商 / 同级,又用了「智商税」「劝退」这类贬义词即拦下;「A 不如 B」这类客观比较不拦
- 用公司事实讲产品参数(只在产品向方向生效)

---

## 品类与内容方向

品类配置在 `app/categories/profiles/`,目前只有 `auto_long.yaml`(汽车横屏解说,`track: explainer`)。

- **方向**(`themes`):一条视频只讲一个方向,当前是 `buyguide`(单车选购深讲)。每个方向声明自己的事实来源、
  所需事实层级、脚本规则和段落块。
- **段落权重铺展**:方向用 `blocks` 声明有哪些段落、各占多重,`plan_for()` 按 时长 / `duration_grid`
  确定性铺展成段落序列;开场等铺垫段落设 `remainder: false`,不抢补位名额。
- **段落**(`sections`):每段的节拍说明、章节名、背景偏外观还是车内、允许的版式、卡片类型。
- 改完 YAML 必须重启服务(`load_profile` 有进程内缓存)。

---

## 两种接口后端:mcode 与 direct

所有外部能力都经过 `providers/cli.call(工具名, 参数)`,`CONNECTOR_BACKEND` 决定它落到哪:

- **`mcode`**:`mcode-tools` CLI,登录由 MiniMax Code 托管,离开 MiniMax Code 就调不通。
- **`direct`**:`providers/direct.py` 用 M Plan 订阅 Key 直连开放平台,返回与 Connector 相同的形状。

| 能力 | direct 实现 | 备注 |
|---|---|---|
| 配音 / BGM / 生图 / 音色 | `/v1/t2a_v2`、`/v1/music_generation`、`/v1/image_generation`、`/v1/get_voice` | 模型由 `TTS_MODEL` / `MUSIC_MODEL` / `IMAGE_MODEL` 指定 |
| 图片理解 | `/v1/coding_plan/vlm` | 素材池打标签用;本地图转 data URI |
| 图片检索 | Bing 图片 | 开放平台没有图片检索接口 |
| 事实:新闻检索 | `/v1/coding_plan/search` | `source_tool` 记为 `minimax.web_search` |
| 事实:同花顺行情财报 / 企查查 | **不支持** | 该来源记失败,其余照常 |

`.env` 填 `MINIMAX_PLAN_KEY`、`CONNECTOR_BACKEND` 留空时:有订阅 Key 且找不到 `mcode-tools` 就走 direct。
自检:`python -m tests.verify_direct`。

⚠️ `/v1/coding_plan/vlm` 与 `/v1/coding_plan/search` 是套餐接口,M Plan 2026-10-31 到期后可能直接不可用。

---

## 成本说明

界面上的金额一律是**等效用量**(`costs.py`,MiniMax 国际站标价折算;BGM、LLM、VLM 单价是粗估),仅用于
项目级节流阈值与套餐到期后的参考,**不是账单**。

- 单条片子:`estimate_explainer` = TTS(约 5.8 字 / 秒)+ BGM(命中曲库为 0)+ 3 次 LLM,留 1.2 倍给门禁 A 改稿重确认。
- 素材池:`estimate_library` = 视觉调用次数 × 单价 + 每 12 张一次 LLM,每台车一次性,项目页单独显示。

---

## 目录结构

```
app/
  config.py          配置、ffmpeg 路径解析
  models.py          Pydantic 领域模型(规格与运行态分离)
  state.py           项目状态机(门禁逻辑)
  costs.py           等效用量估算
  db.py              SQLite 持久化(运行态)+ JSON 文件(可编辑内容)
  llm/               LLM 抽象:MiniMax 优先,否则 DeepSeek
  providers/         外部通道:cli / direct / audio / images / facts / 素材源(clips / official / platform_video / webassets)
  pipeline/
    intake.py            ① 事实采集
    script_gen.py        ② 脚本生成 + 合规 lint
    explainer.py         ③ 逐拍版式、时长、渲染、合成
    cards.py / overlays.py   数据卡、面板、大字、大数字的本地绘制
    library.py           素材池:找源、切段、打标签、铺背景
    vision_verdict.py    视觉模型逐张描述 → 结构化标签
    narration.py         旁白、字幕、BGM 与缓存
    render.py / timeline.py  ffmpeg 合成、时间轴、ASS 字幕
    orchestrator.py      流水线编排
  categories/        品类 Profile:方向库、段落权重铺展、校验
  main.py            FastAPI 应用与后台调度器
  web/               Jinja2 模板 + 原生 JS(零构建链)
```

---

## 测试

```bash
python -m pytest tests/ -q                  # 单元测试,无需网络与额度
python -m tests.verify_templates            # 模板语法 + 样例渲染(抓未定义字段)
python -m tests.verify_llm                  # 真实 LLM 调用自检(消耗 API 额度)
python -m tests.verify_direct               # direct 后端真实调用自检(消耗少量额度)
python -m tests.verify_number_grounding     # 核对脚本数字是否真在它引用的那条事实里
```

---

## 已知限制

- **`mcode-tools` 认证**:只能在 MiniMax Code 里用,别处请用 direct 后端。
- **direct 后端少两样**:同花顺 / 企查查的行情、财报、工商数据只能经 MiniMax Code。
- **素材依赖本机网络**:B站 / 汽车之家国内直连;YouTube、TikTok 需要 `YTDLP_PROXY`;抖音多半需要 `YTDLP_COOKIES`。
  新车、小众车型的官方素材少,池子薄时背景会重复。
- **`app/llm/minimax.py` 未经验证**:配置了 `MINIMAX_API_KEY` 后 `orchestrator._llm()` 会优先用它。
- **多车横评**(多个主体、每车一池)尚未实现。
