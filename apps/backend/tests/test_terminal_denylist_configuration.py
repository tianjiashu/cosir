"""终端 deny-list 配置文件与命令匹配行为。"""

import json

import pytest

from app.core.tools.policy import terminal_denylist_configuration as terminal_reader
from app.core.tools.policy.terminal_denylist import (
    DEFAULT_DENY_PATTERNS,
    compile_deny_patterns,
    detect_dangerous_command,
)
from app.core.tools.policy.terminal_denylist_configuration import (
    TerminalDenylistConfigurationError,
)
from app.core.tools.schemas import ToolExecutionContext
from app.service.configuration import terminal_denylist_configuration_service as config_module
from app.service.configuration.terminal_denylist_configuration_service import (
    TerminalDenylistConfigurationService,
)


@pytest.fixture
def denylist_service(tmp_path, monkeypatch):
    root = tmp_path / ".cosir"
    path = root / "config" / "terminal_denylist.json"
    monkeypatch.setattr(config_module, "system_cosir_dir", lambda: root)
    monkeypatch.setattr(config_module, "system_terminal_denylist_file", lambda: path)
    return TerminalDenylistConfigurationService(), path


def test_missing_file_is_created_with_the_current_default_pattern_list(denylist_service):
    service, path = denylist_service

    document = service.read()

    assert document.patterns == DEFAULT_DENY_PATTERNS
    assert json.loads(path.read_text(encoding="utf-8")) == list(DEFAULT_DENY_PATTERNS)
    assert path.parent.is_dir()


def test_updates_are_read_from_disk_on_the_next_call(denylist_service):
    service, path = denylist_service
    service.update([r"\bmake-danger\b"])

    assert service.read_patterns() == (r"\bmake-danger\b",)
    assert json.loads(path.read_text(encoding="utf-8")) == [r"\bmake-danger\b"]


def test_empty_list_is_a_valid_explicit_configuration(denylist_service):
    service, _ = denylist_service
    service.update([])

    assert service.read_patterns() == ()


def test_process_execution_context_does_not_carry_denylist_configuration(tmp_path):
    context = ToolExecutionContext(
        task_id=1,
        workspace_id=2,
        workspace_root=tmp_path,
    )

    process_context = context.for_process_execution()

    assert not hasattr(process_context, "terminal_deny_patterns")
    assert not hasattr(process_context.runtime_dependencies, "terminal_denylist_reader")


def test_terminal_tool_reader_creates_defaults_then_reads_each_update(tmp_path, monkeypatch):
    path = tmp_path / ".cosir" / "config" / "terminal_denylist.json"
    monkeypatch.setattr(terminal_reader, "system_terminal_denylist_file", lambda: path)

    assert tuple(
        pattern.pattern for pattern in terminal_reader.read_terminal_deny_patterns()
    ) == DEFAULT_DENY_PATTERNS
    assert json.loads(path.read_text(encoding="utf-8")) == list(DEFAULT_DENY_PATTERNS)

    path.write_text(json.dumps([r"\bnew-rule\b"]), encoding="utf-8")
    assert tuple(
        pattern.pattern for pattern in terminal_reader.read_terminal_deny_patterns()
    ) == (r"\bnew-rule\b",)


def test_terminal_tool_reader_fails_closed_on_invalid_file(tmp_path, monkeypatch):
    path = tmp_path / ".cosir" / "config" / "terminal_denylist.json"
    path.parent.mkdir(parents=True)
    path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(terminal_reader, "system_terminal_denylist_file", lambda: path)

    with pytest.raises(ValueError, match="不是有效 JSON"):
        terminal_reader.read_terminal_deny_patterns()


def test_invalid_regex_is_rejected_without_overwriting_saved_configuration(denylist_service):
    service, path = denylist_service
    service.update([r"\brm\b"])

    with pytest.raises(TerminalDenylistConfigurationError, match="无效正则"):
        service.update(["("])

    assert json.loads(path.read_text(encoding="utf-8")) == [r"\brm\b"]


def test_malformed_file_fails_closed_instead_of_using_defaults(denylist_service):
    service, path = denylist_service
    path.parent.mkdir(parents=True)
    path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(TerminalDenylistConfigurationError, match="不是有效 JSON"):
        service.read_patterns()


def test_configured_patterns_apply_to_normalized_commands_and_interpreter_code():
    patterns = compile_deny_patterns(DEFAULT_DENY_PATTERNS)

    assert detect_dangerous_command("r'm' -rf /", patterns).is_dangerous
    assert detect_dangerous_command("python -c \"shutil.rmtree('x')\"", patterns).is_dangerous
    assert detect_dangerous_command('powershell -c "Remove-Item x"', patterns).is_dangerous
    assert detect_dangerous_command('pwsh -e "Zm9v"', patterns).is_dangerous
    assert detect_dangerous_command('pwsh -ec "Zm9v"', patterns).is_dangerous
    assert not detect_dangerous_command("git status", patterns).is_dangerous
    assert not detect_dangerous_command("echo delete file", patterns).is_dangerous
    assert not detect_dangerous_command("echo remove(x)", patterns).is_dangerous
    assert not detect_dangerous_command("echo Remove-Item", patterns).is_dangerous


def test_pathological_pattern_times_out_and_is_not_treated_as_a_match_miss():
    patterns = compile_deny_patterns((r"^(a+)+$",))

    with pytest.raises(TimeoutError):
        detect_dangerous_command("a" * 100_000 + "!", patterns)


def test_command_over_the_evaluation_size_limit_fails_closed():
    patterns = compile_deny_patterns(())

    with pytest.raises(TimeoutError, match="size limit"):
        detect_dangerous_command("x" * 1_000_001, patterns)
