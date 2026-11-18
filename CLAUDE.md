# CLAUDE.md

AutoReel-AI:本地运行的产品解说视频工厂 + AI 团队。项目介绍、依赖和配置见 [README.md](README.md),这里只写"不读会踩坑"的规则。Windows 开发机,回复和计划用中文。

## 常用命令

```bash
pytest                          # 全量测试;默认不跑 probe_*/verify_*(会联网或花额度)
pytest -m "not network and not quota"
ruff check app tests            # line-length 100
python -m mypy                  # 只报 pyproject 里 files 列的模块(db 包、state、models、jobs、locks 等)
npm run check                   # 前端 lint + typecheck + vitest(app/web/static/js)
python run.py --port 8849       # 开发服务(.claude/launch.json 里有 dev / e2e 配置)
```

前四条(pytest 用 `-n auto`)在 PR 和 main 上由 GitHub Actions 跑(`.github/workflows/ci.yml`),提交前本地先跑通。ruff / mypy 在 pyproject 里钉了版本,升级时本地先清干净再改版本号。

## 服务器与调度器(最容易出事)

- **一个库只跑一个服务器。** 8848(用户自用)、8860(`scripts/serve.ps1` 默认)、8849(开发/e2e)共用同一个 `data/AutoReel-AI.db`(`data/aimovice.db` 是 0 字节的旧空文件,别用)。两个调度器会推进同一个项目,导致重复渲染、重复合成,报"后台推进失败"。现在有文件锁兜底(`app/instance_lock.py`,锁文件 `data/<库名>.scheduler.lock`):后启动的服务抢不到锁就只开 Web,不推进、不发布、不跑团队,首页有「只读服务」横幅,`/api/health` 的 `background.ok` 为 false。所以想在 8849 上验证流水线改动,得先停掉占着锁的那个服务。锁只管带这段代码的进程:还在跑旧代码的服务不认锁,启动前仍要先确认端口。
- `scripts/serve.ps1` 会**杀掉已在监听该端口的进程**。曾因此中断用户正在渲染的项目。先用 `curl --noproxy '*' http://127.0.0.1:<port>/` 探测(不加 `--noproxy` 会走代理返回 000)。
- **8848 默认是两个进程**:`run.py --port 8848 --role web`(只开 Web)+ `run.py --role worker`(调度器、jobs、发布、指标、团队,无 HTTP),由 `ensure_server.ps1` 一起看着(`-Mode single` 退回单进程)。两边只经库交流:跨进程催调度器走 `scheduler_kicks` 表(`scheduler.kick` 在没跑调度器的进程里自动改写库),worker 心跳在 `data/<库名>.worker.heartbeat`,停机请求文件 `data/<库名>.worker.stop`。Web 进程里别再起后台线程跑角色 / 渲染,用 `jobs.submit`;临时物 / 缓存清理只在跑后台循环的进程里做(Web 单独重启时清 `data/_tmp` 会删掉 worker 正在用的文件)。
- **重启 8848(比如让新代码生效)只用 `scripts/restart_server.ps1 -Reason "<为什么>"`**(`-Target web|worker|all`,只重启 Web 从不被拒绝),不要 `Stop-Process` 端口进程。它在调度器有项目、有发布在上传、有团队角色在跑时拒绝重启(`-WaitMinutes N` 等它空下来),重启和拒绝都记进 `data/logs/watchdog.log`。2026-11-13 至 11-16 的 14 次"服务重启"里有 9 次是会话手动杀的,日志里看着像服务不稳,还可能打断渲染。服务不应答时它也不动手:卡死归看门狗管。
- **长期跑的 8848 只用 `scripts/ensure_server.ps1` 启动**(stdout/stderr 落到 `data/logs/server-*.log`)。别在会话里 `python run.py` 后台挂着:会话结束后管道没人读,写满就卡死整个进程(2026-11-13 闭环因此停了一天)。控制台日志现已改成队列 + 满了就丢,事件循环卡 60 秒会把全部线程栈写到 `data/logs/hang-*.txt`;ensure_server 对连续约 90 秒不应答的 run.py 会杀掉重启。
- `app/scheduler.py` 的 `Scheduler`(`app.main` 仍重新导出) 每 20 秒自动推进 `AUTO_ADVANCE_STATES`:除门禁、终态、PAUSED_BUDGET 以外的全部状态。分两条通道 —— 渲染(DRAFT_RENDERING / COMPOSING,`PIPELINE_CONCURRENCY` 路)和备料(门禁 A 之前的事实 / 脚本 / 分镜与 ASSETS_READY,`PREP_CONCURRENCY` 路)。建项目后用 `scheduler.kick(pid)` 催它马上开始,别再起一次性线程去跑 `run_until_waiting`(服务重启后没人接手)。这些状态下 `/advance` 只会催调度器(见下),不会再和它抢写文件。只在 GATE_A / GATE_B 调 `/gate`,然后轮询 `/status`。
- 状态的调度属性(自动 / 门禁 / 终态、归哪条通道、下一步)只在 `app/state.py` 的 `SPECS` 声明,调度器的 `AUTO_ADVANCE_STATES` 等集合由它推导,别再手写。新增自动态或门禁态还要在 `orchestrator.STAGES` 登记处理器;声明不自洽模块加载就失败,漏登记 `tests/test_state_specs.py` 会挂。
- 自动态下 `/advance` 不再同步推进,只 `scheduler.kick()` 催调度器,返回 `queued: true`。
- **后台活别再起一次性线程**,用 `app/jobs.py`:`jobs.register(kind, resource=..., max_attempts=...)` 登记 handler(模块加进 `HANDLER_MODULES`),`jobs.submit(kind, key, payload)` 排队。任务落 `jobs` 表,同 kind+key 只有一个在排 / 在跑,服务重启后接着跑(中断不算尝试),所以 handler 必须幂等、payload 只放能 JSON 化的东西。素材池构建(`library.build`)和精修(`refine.apply`)已迁过去;在跑的任务不挡 `restart_server.ps1`。
- 项目写锁(`pipeline/locks.py`)是两层:进程内锁 + 库里的项目租约(`project_leases`,TTL 120 秒,后台续租),所以只读服务上的写接口和主服务的调度器也互斥。进程被强杀后该项目最多 2 分钟内推进不了(等租约过期),正常停机会立刻放掉。调度器的连续失败次数 / 退避窗口在 `projects.advance_failures` / `advance_retry_at`,状态一变就清零。
- `TEAM_ENABLED` 默认开:服务一启动就会按节奏真实调用 LLM、抓平台数据。测试和临时实验服务注意关掉或隔离。

## 观测与分层

- 模型调用账本 `llm_calls`(`app/llm/ledger.py`):文字 / 看图 / 生图每次调用一行,带调用方、项目 / 角色、token、耗时。新增模型通道要在出口调 `ledger.record`;要按项目归账就用 `ledger.tag(project_id=...)` 包住。查看:`/api/llm/usage?by=purpose|model|project_id|role|day`、`/api/llm/calls`。
- 降级路径(概念动画退回数据卡、封面退回老封面、事实源跳过、整段 TTS 退回逐句……)每走一次调 `degrade.note(kind, detail)` 记进 `degradations`,项目自动从账本上下文取。新加降级分支也要记,只写日志等于看不见。查看:`/api/degradations`,`/api/health` 的 `degradations_24h`。
- `tests/test_layers.py` 守模块分层(下层不许 import 上层,含函数内的延迟 import)。新模块要在 `RANK` 里定层;`KNOWN` 里的旧例外只许减不许增。
- 启动时检查 `.env` 的重复键 / 空值覆盖(`config.env_file_problems`),结果进日志、首页横幅和 `/api/health` 的 `env`。

## 测试

- `tests/conftest.py` 的 autouse fixture 已把 `DATA_DIR` 指到临时目录;别用 `monkeypatch.undo()`(会连它一起撤掉,改用 `monkeypatch.context()`)。测试里调用 `orchestrator.create_project` 等会写库的函数,仍**建议用临时库 fixture**(参考 `tests/test_activator.py` 的 `fresh_db`)。曾有测试把行漏进真实的库(现在是 `data/AutoReel-AI.db`)。
- `tests/conftest.py` 已把 BGM 曲库重定向到 tmp,别绕开。
- 提示词有黄金快照 `tests/golden/auto_long_prompts.json`。改 prompt 后用 `tests/test_profile_vocab.py` 里的 `regen_golden` 重生成,并在 diff 里确认只有预期变化。
- `playbook.install()` 只在 `team.scheduler.start()` 里调用,所以测试和黄金快照不受学习权重影响。别在别处调用它。
- 不要给临时脚本起标准库同名(`tokenize.py` 之类):会遮蔽 stdlib,曾导致迁移被重跑。
- `probe_*.py` 是联网探针,`verify_*.py` 会烧真实额度,都不要当成普通测试跑。

## Git

- 工作区常有多个会话同时改动。提交时只带自己的改动(`git add -p` / `git apply --cached`),别 `git add -A`。
- `commit-tree` 提交后索引可能过期:看差异用 `git diff HEAD`,不要用裸 `git diff`。
- 不提交:`data/`、`.env`、`cookies/`、根目录的演示 mp4。`douyin_bg/` 故意不入库。

## 流水线设计约束

- **脚本里每个数字都必须引用一条事实**(`Fact`,带 `basis` 口径、`source_kind`)。不要让 LLM 或记忆类工具产出没有来源的数字。新增 lint 时遵循现有的 error / warning 分级。
- 数字一律写阿拉伯数字(`grounding.arabicize` 兜底),字幕用原文、TTS 用 `normalize_for_tts`。
- 只有 `track: explainer` 一种轨道,横屏 / 竖屏由 `projects.aspect` 决定,创建后不可改,统一走 `pipeline/aspect.py`。竖屏回归时旧的"事后转竖屏"已删除,别重新加回。
- 品类文本放在 YAML `vocab:`,引擎里不要写死"车"。`auto_long` 的数据在 `app/categories/profiles/`。
- 压缩时长靠测得的语速:横屏 `LANDSCAPE_CHARS_PER_SECOND`=5.7,竖屏 4.7 字/秒。`costs.py` 仍按 5.8 保守估算。

## 渲染与音频

- 合成前先单独混音到 `_render/mix.wav`,再让主命令映射它。**不要把 loudnorm 放回视频拼接的同一条 ffmpeg 命令**(ffmpeg 9 会丢约 3 秒的音频块)。放行前有 `render.audio_problem` 数包检查。
- 解码完整性检查:`ffmpeg -v error -i x.mp4 -f null -`。
- 概念动画用 HyperFrames,需要 **node 22+**(`config.motion_node()` 会在 `NVM_HOME` 下找)。全局 node 是 16 时概念动画会静默退回数据卡。GPU 挂起时 `MOTION_BROWSER_GPU=off` 强制软渲染。
- `motion/` 里模版只 ASCII,外部 JS(`_lib.js`、`_viz.js`)不受此限。HyperFrames 只内嵌页面内声明的字体。
- BGM 混音对旁白的闪避曾淹没解说;现 `BGM_ENABLED` 的行为见 `render._build_audio_graph`,改动后要验证旁白相关性。

## 平台抓取与发布

- **B站空间 API 限流很严**:几次探测就会 412 / -352 持续 30 分钟以上。测试时别连续调用;失败时素材发现会降级为关键词搜索。被风控后 `providers/cooldown.py` 记冷却(`cooldowns` 表,连续被拦翻倍,跨进程 / 重启有效),冷却期内不发请求;抖音搜索页撞验证码同理(`douyin.search`,6 小时)。新接一个会被风控的接口也用 `cooldown.check / trip`,别在内存里自己记。冷却中的在 `/api/health` 的 `cooldowns`,确认恢复后 `DELETE /api/cooldowns/<key>`。浏览器兜底(`browser_fetch`)同一站点连续失败 2 次也登记冷却(`browser.<host>`,1 小时);撞验证码的站在 `blocked_sites`,人工 `browser_unblock` 后拦截和冷却一起清。
- 不要抓懂车帝的参数配置页(`auto/params-carIds-*`),返回空 200,疑似反爬。图库需要登录,状态在 `data/_browser/dongchedi.json`。
- 浏览器会话共用 profile,同一 profile 在进程内有锁(`BrowserSession`);`BROWSER_PATH` 默认是 chrome-headless-shell,需要有界面登录时用 `publish/browser.headed_browser` 换真 Chrome。
- 抖音评论回复的真实发送路径、小红书图文上传(`_publish_note`)、对标账号抓取,都**没在真实账号上完整跑过**。首次启用要盯着。
- 对标号自动发现(`team/benchmark_discover.py`,抖音搜索页)2026-11-06 实测:无头浏览器打开搜索页会跳「验证码中间页」,不绕过;发现失败会降级为手动添加对标号。别连续重试。
- 登录态过期时 analyst 会阻塞浏览器,直到 `douyin.json` 的 mtime 变化。

## Windows 环境

- `python -c` 里的中文参数会乱码:写成 UTF-8 脚本文件再运行。
- `.ps1` 文件保持纯 ASCII(PowerShell 5.1 会按 ANSI 读),说明写进 README。
- `.env` 里重复的空行 `KEY=` 会覆盖真实值。启动自检会报出来(首页横幅),改完 `.env` 看一眼。
- ffmpeg 不在 PATH,在 `.env` 的 `FFMPEG_BIN` 指定。

## 外部服务依赖

- TTS / 看图 / 搜索 / 专业数据集走火山方舟 Agent Plan 专属 Key(`app/providers/ark_*.py`)。文字工作仍用 `DeepSeekProvider`,但 2026-11-16 起默认经方舟调 `deepseek-v4.1-flash`(`config.text_llm()`,`TEXT_LLM=deepseek` 退回官方接口)。代码里已无 `MINIMAX_PLAN_KEY` 引用,别再按 MiniMax 通道写新功能。
- MiniMax Music 曾对新用户返回 410;BGM 来自 `data/bgm/` 与 Mixkit 自动采集(`bgm_source.py`)。
