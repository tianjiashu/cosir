"""真实 PTY/ConPTY 行为验证；按实际安装的 shell 显式跳过不可验证的平台。"""

import os
import shutil
from pathlib import Path

import pytest

from app.config.constant import Constant
from app.service.terminal.terminal_session_service import TerminalSessionService


@pytest.fixture(params=["zsh", "powershell", "pwsh"])
def terminal(request, tmp_path, monkeypatch):
    shell = request.param
    if shell == "zsh" and os.name == "nt":
        pytest.skip("macOS/Unix zsh requires a Unix PTY")
    if shell != "zsh" and os.name != "nt":
        pytest.skip("Windows PowerShell/ConPTY requires Windows")
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    root = Path(__file__).resolve().parents[3]
    binary = (
        root
        / "target"
        / "debug"
        / ("terminal-worker.exe" if os.name == "nt" else "terminal-worker")
    )
    if not binary.is_file():
        pytest.skip("terminal-worker must be built")
    # worker 路径只有宿主环境变量一个来源（生产由 Tauri spawn_backend 注入），
    # 测试同样经由该边界配置，不再从 service 构造参数注入。
    monkeypatch.setenv(Constant.Terminal.WORKER_ENV, str(binary))
    service = TerminalSessionService(status_observer=lambda _change: None)
    snapshot = service.start(
        task_id=1, workspace_id=1, workspace_root=str(tmp_path), run_id=1, shell=shell
    )
    session = snapshot["session_id"]
    try:
        ready = service.read(session, task_id=1, wait={"until": "shell_prompt", "timeout_ms": 5000})
        assert ready["integration"]["status"] == "ready", ready
        yield service, session, shell
    finally:
        service.shutdown()


def write(service, session, text, operation):
    return service.write(
        session,
        task_id=1,
        operation_id=operation,
        input={"type": "line", "text": text},
        wait_ms=500,
    )


def text(page):
    return "".join(event["text"] for event in page["events"] if event["type"] == "output")


def started(page):
    return [event for event in page["events"] if event["type"] == "command_started"]


def returned(page):
    return [event for event in page["events"] if event["type"] == "command_returned"]


def test_repl_answers_do_not_create_or_complete_shell_commands(terminal):
    service, session, shell = terminal
    python = "python" if os.name == "nt" else "python3"
    first = write(service, session, python + " -q", "start-python")
    assert len(started(first)) == 1, first
    command = started(first)[0]["command_id"]
    assert not returned(first), first
    second = write(service, session, "print(6 * 7)", "answer")
    assert "42" in text(second), second
    assert not started(second) and not returned(second), second
    waiting = service.read(
        session,
        task_id=1,
        cursor=second["next_cursor"],
        wait={"until": "command_returned", "command_id": command, "timeout_ms": 100},
    )
    assert waiting["return_reason"] == "timeout"
    final = write(service, session, "exit()", "exit-python")
    if not returned(final):
        final = service.read(
            session,
            task_id=1,
            cursor=final["next_cursor"],
            wait={"until": "command_returned", "command_id": command, "timeout_ms": 3000},
        )
    assert final["state"]["active_command_id"] is None, final
    done = returned(final) or [final["condition"]]
    assert done[0]["command_id"] == command


def test_silent_execution_wait_interrupt_and_reuse(terminal):
    service, session, shell = terminal
    command_text = "sleep 10" if shell == "zsh" else "Start-Sleep -Seconds 10"
    first = write(service, session, command_text, "sleep")
    command = started(first)[0]["command_id"]
    waiting = service.read(
        session,
        task_id=1,
        cursor=first["next_cursor"],
        wait={"until": "command_returned", "command_id": command, "timeout_ms": 150},
    )
    assert waiting["return_reason"] == "timeout"
    service.signal(session, task_id=1, signal_name="interrupt")
    final = service.read(
        session,
        task_id=1,
        cursor=waiting["next_cursor"],
        wait={"until": "command_returned", "command_id": command, "timeout_ms": 3000},
    )
    assert final["return_reason"] == "command_returned", final
    next_page = write(service, session, "echo usable", "reuse")
    assert "usable" in text(next_page)


def test_state_persists_and_failed_result_is_not_reused(terminal):
    service, session, shell = terminal
    set_value = "export COSIR_TEST_VALUE=kept" if shell == "zsh" else '$env:COSIR_TEST_VALUE="kept"'
    first = write(service, session, set_value, "set")
    assert returned(first)[0]["result"]["success"] is True, first
    command = (
        'printf "VALUE:%s\\n" "$COSIR_TEST_VALUE"'
        if shell == "zsh"
        else 'Write-Output "VALUE:$env:COSIR_TEST_VALUE"'
    )
    second = write(service, session, command, "read")
    assert "VALUE:kept" in text(second), second
    failure = write(
        service,
        session,
        "false" if shell == "zsh" else "cmd /c exit 7",
        "fail",
    )
    assert returned(failure)[0]["result"]["success"] is False, failure
    success = write(service, session, "echo okay", "succeed")
    assert returned(success)[0]["result"]["success"] is True, success
    if shell != "zsh":
        assert returned(success)[0]["result"]["code"] is None


def test_raw_preview_is_independent_of_agent_cursor(terminal):
    service, session, _ = terminal
    preview = service.subscribe(session, task_id=1, after_seq=None)
    preview.subscription.activate()
    try:
        page = write(service, session, "echo preview", "preview")
        assert "preview" in text(page)
        event = preview.subscription.get(1)
        assert event["type"] == "output" and "data_base64" in event
        assert event["seq"] >= 1
    finally:
        service.unsubscribe(session, preview.subscription)


def test_multiline_editing_empty_enter_and_prompt_text_are_not_execution(terminal):
    service, session, shell = terminal
    first_line = "for n in 1 2; do" if shell == "zsh" else "foreach ($n in 1,2) {"
    partial = write(service, session, first_line, "multiline-start")
    assert not started(partial) and not returned(partial), partial
    final_line = "echo $n; done" if shell == "zsh" else "Write-Output $n }"
    whole = write(service, session, final_line, "multiline-end")
    assert len(started(whole)) == len(returned(whole)) == 1, whole
    empty = service.write(
        session,
        task_id=1,
        operation_id="empty",
        input={"type": "line", "text": ""},
    )
    assert not started(empty) and not returned(empty), empty
    # 普通输出伪装成提示符不会改变 shell 命令观察。
    python = "python" if os.name == "nt" else "python3"
    active = write(service, session, python + " -q", "python")
    assert len(started(active)) == 1 and not returned(active), active
    prompt = write(service, session, 'print("PS C:\\ fake> % $ >>>")', "fake-prompt")
    assert not started(prompt) and not returned(prompt), prompt


def test_program_question_and_two_commands_keep_distinct_boundaries(terminal):
    service, session, shell = terminal
    python = "python" if os.name == "nt" else "python3"
    command = python + " -c \"print(input('Continue? '))\""
    question = write(service, session, command, "question")
    identity = started(question)[0]["command_id"]
    assert not returned(question)
    answer = write(service, session, "yes", "answer")
    assert not started(answer), answer
    assert returned(answer)[0]["command_id"] == identity, answer
    next_command = write(service, session, "echo second", "second")
    assert started(next_command)[0]["command_id"] != identity
    old_wait = service.read(
        session,
        task_id=1,
        cursor=next_command["next_cursor"],
        wait={"until": "command_returned", "command_id": identity, "timeout_ms": 0},
    )
    assert old_wait["condition"]["command_id"] == identity


def test_zsh_syntax_error_background_and_nested_shell(terminal):
    service, session, shell = terminal
    if shell != "zsh":
        pytest.skip("zsh job-control semantics")
    good = write(service, session, "true", "good")
    assert returned(good)[0]["result"]["success"] is True, good
    bad = write(service, session, ")", "syntax")
    assert not started(bad) and not returned(bad), bad
    background = write(service, session, "sleep 10 &", "background")
    assert len(returned(background)) == 1
    nested = write(service, session, "zsh -f", "nested")
    command = started(nested)[0]["command_id"]
    child = write(service, session, "echo nested", "child-command")
    assert not started(child) and not returned(child), child
    final = write(service, session, "exit", "child-exit")
    assert returned(final)[0]["command_id"] == command


def test_powershell_manual_prompt_does_not_return_early(terminal):
    service, session, shell = terminal
    if shell == "zsh":
        pytest.skip("PowerShell prompt wrapper")
    page = write(service, session, "prompt; Start-Sleep -Seconds 2", "manual-prompt")
    assert len(started(page)) == 1
    assert not returned(page), page


@pytest.mark.skipif(os.name == "nt" or shutil.which("zsh") is None, reason="zsh startup chain")
def test_zsh_keeps_user_configuration_and_can_disable_integration(tmp_path, monkeypatch):
    configuration = tmp_path / "config"
    configuration.mkdir()
    (configuration / ".zshenv").write_text('export COSIR_START_CHAIN="env"\n')
    (configuration / ".zshrc").write_text('export COSIR_START_CHAIN="$COSIR_START_CHAIN-rc"\n')
    monkeypatch.setenv("ZDOTDIR", str(configuration))
    binary = Path(__file__).resolve().parents[3] / "target/debug/terminal-worker"
    if not binary.is_file():
        pytest.skip("terminal-worker must be built")
    monkeypatch.setenv(Constant.Terminal.WORKER_ENV, str(binary))
    service = TerminalSessionService(status_observer=lambda _change: None)
    try:
        for index, integration in enumerate(["auto", "off"]):
            snapshot = service.start(  # noqa: S604 - 领域 shell 名称，不是 subprocess shell=True。
                task_id=1,
                workspace_id=1,
                workspace_root=str(tmp_path),
                run_id=1,
                shell="zsh",
                integration=integration,
            )
            page = write(
                service, snapshot["session_id"], "echo CHAIN:$COSIR_START_CHAIN", f"chain-{index}"
            )
            assert "CHAIN:env-rc" in text(page), page
            assert page["integration"]["status"] == ("ready" if integration == "auto" else "off")
            if integration == "off":
                assert not started(page) and not returned(page)
    finally:
        service.shutdown()


def test_python_multiline_needs_explicit_blank_line_and_keeps_root_command(terminal):
    service, session, _ = terminal
    python = "python" if os.name == "nt" else "python3"
    launch = write(service, session, python + " -q", "python")
    identity = started(launch)[0]["command_id"]
    first = write(service, session, "for n in range(2):", "header")
    body = write(service, session, "    print('MULTI_RESULT', n)", "body")
    for page in [first, body]:
        assert not started(page) and not returned(page), page
        assert page["state"]["active_command_id"] == identity
        assert "MULTI_RESULT 0" not in text(page), page
    done = write(service, session, "", "blank")
    assert "MULTI_RESULT 0" in text(done) and "MULTI_RESULT 1" in text(done), done
    assert not started(done) and not returned(done), done
    final = write(service, session, "exit()", "exit")
    assert returned(final)[0]["command_id"] == identity, final


def test_program_answers_preserve_spaces_and_allow_empty_line(terminal):
    service, session, _ = terminal
    python = "python" if os.name == "nt" else "python3"
    question = write(
        service, session, python + ' -c "print(repr(input())); print(repr(input()))"', "ask"
    )
    identity = started(question)[0]["command_id"]
    first = write(service, session, "  yes  ", "spaces")
    assert "'  yes  '" in text(first), first
    assert not started(first) and not returned(first), first
    second = write(service, session, "", "default")
    assert "''" in text(second), second
    assert not started(second), second
    assert returned(second)[0]["command_id"] == identity, second


def test_control_key_interrupts_python_and_eof_returns_to_shell(terminal):
    service, session, _ = terminal
    python = "python" if os.name == "nt" else "python3"
    launch = write(service, session, python + " -q", "python")
    identity = started(launch)[0]["command_id"]
    busy = write(service, session, "import time; time.sleep(30)", "busy")
    assert not returned(busy)
    interrupt = service.write(
        session,
        task_id=1,
        operation_id="interrupt",
        input={"type": "key", "key": "ctrl_c"},
        wait_ms=500,
    )
    assert "KeyboardInterrupt" in text(interrupt), interrupt
    assert not started(interrupt) and not returned(interrupt), interrupt
    key = "ctrl_z" if os.name == "nt" else "ctrl_d"
    end = service.write(
        session, task_id=1, operation_id="eof", input={"type": "key", "key": key}, wait_ms=500
    )
    if os.name == "nt" and not returned(end):
        end = write(service, session, "", "eof-enter")
    assert returned(end)[0]["command_id"] == identity, end
    usable = write(service, session, "echo usable", "reuse")
    assert "usable" in text(usable), usable
