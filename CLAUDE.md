# CLAUDE.md

AutoReel-AI:本地运行的产品解说视频工厂 + AI 团队。项目介绍、依赖和配置见 [README.md](README.md),这里只写"不读会踩坑"的规则。Windows 开发机,回复和计划用中文。

## 常用命令

```bash
pytest                          # 全量测试;默认不跑 probe_*/verify_*(会联网或花额度)
pytest -m "not network and not quota"
ruff check app tests            # line-length 100
mypy                            # 只覆盖 models.py / state.py / db.py
npm run check                   # 前端 lint + typecheck + vitest(app/web/static/js)
python run.py --port 8849       # 开发服务(.claude/launch.json 里有 dev / e2e 配置)
```

## 服务器与调度器(最容易出事)

- **一个库只跑一个服务器。** 8848(用户自用)、8860(`scripts/serve.ps1` 默认)、8849(开发/e2e)共用同一个 `data/aimovice.db`。两个调度器会推进同一个项目,导致重复渲染、重复合成,报"后台推进失败"。启动前先确认端口。
- `scripts/serve.ps1` 会**杀掉已在监听该端口的进程**。曾因此中断用户正在渲染的项目。先用 `curl --noproxy '*' http://127.0.0.1:<port>/` 探测(不加 `--noproxy` 会走代理返回 000)。
- `app/scheduler.py` 的 `Scheduler`(`app.main` 仍重新导出) 每 20 秒自动推进 `AUTO_ADVANCE_STATES`(DRAFT_RENDERING / FINAL_RENDERING / COMPOSING / QA_PASSED 等)。**这些状态下不要手动调 `/advance`**:会和调度器抢着写同一个文件,产出损坏的 mp4,项目翻成 FAILED。只在 GATE_A / GATE_B 调 `/gate`,然后轮询 `/status`。
- 新增会自动推进的状态,必须加进 `AUTO_ADVANCE_STATES`,漏了就是"静静卡住",没有报错。
- `TEAM_ENABLED` 默认开:服务一启动就会按节奏真实调用 LLM、抓平台数据。测试和临时实验服务注意关掉或隔离。

## 测试

- 测试里调用 `orchestrator.create_project` 等会写库的函数,**必须用临时库 fixture**(参考 `tests/test_activator.py` 的 `fresh_db`)。曾有测试把行漏进真实的 `data/aimovice.db`。
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

- **B站空间 API 限流很严**:几次探测就会 412 / -352 持续 30 分钟以上。测试时别连续调用;失败时素材发现会降级为关键词搜索。
- 不要抓懂车帝的参数配置页(`auto/params-carIds-*`),返回空 200,疑似反爬。图库需要登录,状态在 `data/_browser/dongchedi.json`。
- 浏览器会话共用 profile,同一 profile 在进程内有锁(`BrowserSession`);`BROWSER_PATH` 默认是 chrome-headless-shell,需要有界面登录时用 `publish/browser.headed_browser` 换真 Chrome。
- 抖音评论回复的真实发送路径、小红书图文上传(`_publish_note`)、对标账号抓取,都**没在真实账号上完整跑过**。首次启用要盯着。
- 登录态过期时 analyst 会阻塞浏览器,直到 `douyin.json` 的 mtime 变化。

## Windows 环境

- `python -c` 里的中文参数会乱码:写成 UTF-8 脚本文件再运行。
- `.ps1` 文件保持纯 ASCII(PowerShell 5.1 会按 ANSI 读),说明写进 README。
- `.env` 里重复的空行 `KEY=` 会覆盖真实值,改 `.env` 后检查是否有重复键。
- ffmpeg 不在 PATH,在 `.env` 的 `FFMPEG_BIN` 指定。

## 外部服务依赖

- TTS / 看图 / 搜索 / 专业数据集走火山方舟 Agent Plan 专属 Key(`app/providers/ark_*.py`);文字工作走 DeepSeek。代码里已无 `MINIMAX_PLAN_KEY` 引用,别再按 MiniMax 通道写新功能。
- MiniMax Music 曾对新用户返回 410;BGM 来自 `data/bgm/` 与 Mixkit 自动采集(`bgm_source.py`)。
