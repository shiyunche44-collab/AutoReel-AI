"""`.env` 自检(`config.env_file_problems`):重复键、空值覆盖、键名带空格。"""
from __future__ import annotations

from app import config


def _check(tmp_path, text: str) -> list[str]:
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return config.env_file_problems(p)


def test_clean_file_has_no_problems(tmp_path):
    assert _check(tmp_path, "# 注释\nARK_API_KEY=abc\n\nexport TEXT_LLM=ark\nX=\n") == []


def test_missing_file_is_fine(tmp_path):
    assert config.env_file_problems(tmp_path / "nope") == []


def test_empty_duplicate_overriding_a_real_value(tmp_path):
    [p] = _check(tmp_path, "ARK_API_KEY=secret-value\nTEAM_ENABLED=1\nARK_API_KEY=\n")
    assert "ARK_API_KEY" in p and "第 1、3 行" in p and "覆盖成空" in p
    assert "secret-value" not in p                      # 值可能是密钥,绝不进提示


def test_plain_duplicate_reports_which_line_wins(tmp_path):
    [p] = _check(tmp_path, "TEAM_SLOT_TIMES=18:30\nTEAM_SLOT_TIMES='12:00' # 午间\n")
    assert "生效的是第 2 行" in p


def test_malformed_lines(tmp_path):
    problems = _check(tmp_path, "JUST_A_WORD\nBAD KEY=1\n")
    assert any("第 1 行" in p and "KEY=VALUE" in p for p in problems)
    assert any("第 2 行" in p and "带空格" in p for p in problems)
