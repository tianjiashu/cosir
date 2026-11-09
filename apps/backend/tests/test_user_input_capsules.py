"""用户输入胶囊在入队边界的校验：file / ref 的磁盘事实、Skill 路径补全与错误码归属。"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.assistant_transport.service.user_input_service as user_input_module
from app.assistant_transport.service.user_input_service import UserInputService
from app.core.user_input.capsules import FileCapsule, serialize_capsule
from app.models.conversation_run_command import ConversationRunCommand
from app.models.errors.input_errors import UserInputRejectedError
from tests.model_config_fixtures import model_configs

_TASK_ID = 7


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _service(monkeypatch: pytest.MonkeyPatch, workspace_root: Path) -> UserInputService:
    """不触达存储的 ``UserInputService``：Task 归属 workspace 7，模型不支持图片。"""
    monkeypatch.setattr(
        user_input_module,
        "get_model_config_service",
        lambda: model_configs(
            lambda _id: SimpleNamespace(supports_reasoning_effort=False, supports_image=False)
        ),
    )
    monkeypatch.setattr(
        user_input_module,
        "get_workspace_service",
        lambda: SimpleNamespace(
            get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(workspace_root))
        ),
    )
    service = UserInputService.__new__(UserInputService)
    service._task = SimpleNamespace(get=lambda _task_id: SimpleNamespace(workspace_id=7))
    return service


def _prepare(service: UserInputService, text: str):
    return service.prepare(_TASK_ID, ConversationRunCommand(text=text), 1)


def _write_skill(root: Path, name: str, *, enabled: list[str]) -> None:
    directory = root / ".cosir" / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\n正文\n", encoding="utf-8"
    )
    config = root / ".cosir" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "skills.json").write_text(
        '{"enabled_skills": [' + ", ".join(f'"{item}"' for item in enabled) + "]}",
        encoding="utf-8",
    )


# ── file 胶囊 ─────────────────────────────────────────────────────────────


def test_file_capsule_accepts_existing_file(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    path = workspace / "notes.md"
    path.write_text("hi", encoding="utf-8")

    body = _prepare(_service(monkeypatch, workspace), f'看 <file path="{path}" />').body

    assert body.text == f'看 <file path="{path}" />'
    assert body.presentation.text == body.text


def test_file_capsule_rejects_relative_path(workspace: Path, monkeypatch) -> None:
    """``<file>`` 只接受本机绝对路径：相对路径会随后端进程 cwd 漂移，直接拒绝而不是猜。"""
    workspace.mkdir()
    (workspace / "notes.md").write_text("hi", encoding="utf-8")
    monkeypatch.chdir(workspace)

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), '看 <file path="notes.md" />')

    assert caught.value.code == "CAPSULE_FILE_UNAVAILABLE"


def test_file_capsule_accepts_directory_with_trailing_slash(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    sub = workspace / "src"
    sub.mkdir()

    body = _prepare(_service(monkeypatch, workspace), f'看 <file path="{sub}/" />').body

    assert body.text == f'看 <file path="{sub}/" />'


def test_file_capsule_rejects_directory_without_trailing_slash(
    workspace: Path, monkeypatch
) -> None:
    workspace.mkdir()

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), f'<file path="{workspace}" />')

    assert caught.value.code == "CAPSULE_FILE_UNAVAILABLE"


def test_file_capsule_rejects_file_with_trailing_slash(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    path = workspace / "notes.md"
    path.write_text("hi", encoding="utf-8")

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), f'<file path="{path}/" />')

    assert caught.value.code == "CAPSULE_FILE_UNAVAILABLE"


def test_missing_file_capsule_is_reported_as_unavailable(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), f'<file path="{workspace / "gone.md"}" />')

    assert caught.value.code == "CAPSULE_FILE_UNAVAILABLE"


def test_unreadable_file_capsule_is_not_reported_as_missing(workspace: Path, monkeypatch) -> None:
    """权限或 IO 失败必须报「无法访问」：说成不存在会误导用户重新选择一个仍有效的文件。"""
    workspace.mkdir()
    path = workspace / "locked.md"
    path.write_text("hi", encoding="utf-8")
    real_stat = os.stat

    def fake_stat(target: object, *args: object, **kwargs: object) -> os.stat_result:
        if str(target) == str(path):
            raise PermissionError("denied")
        return real_stat(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(user_input_module.os, "stat", fake_stat)

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), f'<file path="{path}" />')

    assert caught.value.code == "CAPSULE_FILE_UNREADABLE"


def test_file_capsule_attribute_xml_escaping_round_trips(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    path = workspace / 'a & b "q".md'
    path.write_text("hi", encoding="utf-8")

    text = f"看 {serialize_capsule(FileCapsule(path=str(path)))}"
    body = _prepare(_service(monkeypatch, workspace), text).body

    assert body.text == f'看 <file path="{workspace}/a &amp; b &quot;q&quot;.md" />'


# ── ref 胶囊 ──────────────────────────────────────────────────────────────


def test_ref_capsule_accepts_workspace_relative_path(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    target = workspace / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")

    body = _prepare(
        _service(monkeypatch, workspace), '检查 <ref path="src/a.py" lines="12-14" />'
    ).body

    assert body.text == '检查 <ref path="src/a.py" lines="12-14" />'


def test_ref_capsule_accepts_workspace_root_as_empty_path(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()

    body = _prepare(_service(monkeypatch, workspace), '<ref path="" />').body

    assert body.text == '<ref path="" />'


def test_missing_ref_capsule_is_reported_as_unavailable(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), '<ref path="src/gone.py" />')

    assert caught.value.code == "CAPSULE_REF_UNAVAILABLE"


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "src/../../outside.py"])
def test_ref_capsule_rejects_absolute_and_escaping_paths(
    workspace: Path, monkeypatch, path: str
) -> None:
    workspace.mkdir()

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), f'<ref path="{path}" />')

    assert caught.value.code == "CAPSULE_REF_UNAVAILABLE"


# ── skill 胶囊 ────────────────────────────────────────────────────────────


def test_skill_capsule_writes_back_the_skill_file_path(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    _write_skill(workspace, "review", enabled=["review"])
    expected = (workspace / ".cosir" / "skills" / "review" / "SKILL.md").resolve()

    body = _prepare(_service(monkeypatch, workspace), '用 <skill name="review" /> 检查').body

    assert body.text == f'用 <skill name="review" path="{expected}" /> 检查'
    assert body.presentation.text == body.text


@pytest.mark.parametrize("name", ["off", "missing"])
def test_unavailable_skill_is_rejected_before_enqueue(
    workspace: Path, monkeypatch, name: str
) -> None:
    workspace.mkdir()
    _write_skill(workspace, "off", enabled=["review"])

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), f'用 <skill name="{name}" />')

    assert caught.value.code == "SKILL_UNAVAILABLE"


# ── 语法与空输入 ──────────────────────────────────────────────────────────


def test_code_block_capsules_stay_literal_text(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    text = '```\n<file path="/missing" />\n<ref path="../outside" />\n```'

    body = _prepare(_service(monkeypatch, workspace), text).body

    assert body.text == text


def test_invalid_capsule_syntax_stays_literal_text(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    text = '<file path="/missing"> 与 <ref path="a" lines="x" />'

    body = _prepare(_service(monkeypatch, workspace), text).body

    assert body.text == text


def test_capsule_only_input_is_not_empty(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    path = workspace / "notes.md"
    path.write_text("hi", encoding="utf-8")

    body = _prepare(_service(monkeypatch, workspace), f'<file path="{path}" />').body

    assert body.text == f'<file path="{path}" />'


def test_blank_input_is_rejected(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), "  \n ")

    assert caught.value.code == "EMPTY_INPUT"
