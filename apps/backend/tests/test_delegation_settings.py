"""委派子Agent并发执行配置（Settings.DELEGATION_*）的单元测试。

覆盖 Phase 1A 新增的进程级并发配置：默认值、override 合法性、_validate 对非法值的
校验，以及 env 覆盖在 load() 时生效。测试改动 Settings 全局静态属性，均以
try/finally 恢复原值，避免跨用例污染。
"""

import pytest

from app.config.settings import Settings


def test_delegation_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设置 CODING_AGENT_DELEGATION_* 环境变量时，默认值应为 2 / 300.0。"""

    monkeypatch.delenv("CODING_AGENT_DELEGATION_MAX_CONCURRENCY", raising=False)
    monkeypatch.delenv("CODING_AGENT_DELEGATION_TIMEOUT_SECONDS", raising=False)
    original_max = Settings.DELEGATION_MAX_CONCURRENCY
    original_timeout = Settings.DELEGATION_TIMEOUT_SECONDS
    try:
        Settings.load()
        assert Settings.DELEGATION_MAX_CONCURRENCY == 2
        assert Settings.DELEGATION_TIMEOUT_SECONDS == 300.0
    finally:
        Settings.override(
            DELEGATION_MAX_CONCURRENCY=original_max,
            DELEGATION_TIMEOUT_SECONDS=original_timeout,
        )


def test_override_max_concurrency_one_is_valid() -> None:
    """override(DELEGATION_MAX_CONCURRENCY=1) 合法，_validate 不应抛错。"""

    original = Settings.DELEGATION_MAX_CONCURRENCY
    try:
        Settings.override(DELEGATION_MAX_CONCURRENCY=1)
        assert Settings.DELEGATION_MAX_CONCURRENCY == 1
        Settings._validate()
    finally:
        Settings.override(DELEGATION_MAX_CONCURRENCY=original)


@pytest.mark.parametrize("invalid_max_concurrency", [0, -1])
def test_validate_rejects_max_concurrency_below_one(invalid_max_concurrency: int) -> None:
    """DELEGATION_MAX_CONCURRENCY 小于 1 时 _validate 应抛 ValueError。"""

    original = Settings.DELEGATION_MAX_CONCURRENCY
    try:
        Settings.override(DELEGATION_MAX_CONCURRENCY=invalid_max_concurrency)
        with pytest.raises(ValueError, match="DELEGATION_MAX_CONCURRENCY"):
            Settings._validate()
    finally:
        Settings.override(DELEGATION_MAX_CONCURRENCY=original)


@pytest.mark.parametrize("invalid_timeout", [0.0, -1.0])
def test_validate_rejects_non_positive_timeout(invalid_timeout: float) -> None:
    """DELEGATION_TIMEOUT_SECONDS 不大于 0 时 _validate 应抛 ValueError。"""

    original = Settings.DELEGATION_TIMEOUT_SECONDS
    try:
        Settings.override(DELEGATION_TIMEOUT_SECONDS=invalid_timeout)
        with pytest.raises(ValueError, match="DELEGATION_TIMEOUT_SECONDS"):
            Settings._validate()
    finally:
        Settings.override(DELEGATION_TIMEOUT_SECONDS=original)


def test_env_override_applies_on_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """设置 CODING_AGENT_DELEGATION_* 环境变量后，load() 应读取并覆盖默认值。"""

    monkeypatch.setenv("CODING_AGENT_DELEGATION_MAX_CONCURRENCY", "5")
    monkeypatch.setenv("CODING_AGENT_DELEGATION_TIMEOUT_SECONDS", "120.5")
    original_max = Settings.DELEGATION_MAX_CONCURRENCY
    original_timeout = Settings.DELEGATION_TIMEOUT_SECONDS
    try:
        Settings.load()
        assert Settings.DELEGATION_MAX_CONCURRENCY == 5
        assert Settings.DELEGATION_TIMEOUT_SECONDS == 120.5
    finally:
        Settings.override(
            DELEGATION_MAX_CONCURRENCY=original_max,
            DELEGATION_TIMEOUT_SECONDS=original_timeout,
        )
