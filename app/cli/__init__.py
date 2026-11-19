"""Claude Code 用的命令行工具层:`python -m app.cli <命令>`。

Claude 负责判断与写作,这里负责事实来源、硬校验和重活。输出一律是 JSON(stdout),
退出码:0 成功;2 流水线拒绝(lint / 门禁 / 状态不对);3 要 Claude 先回答请求;1 其它错误。
用法见 `.claude/skills/make-video/SKILL.md`。
"""
