"""用户输入胶囊在入队边界的校验：file / ref 的磁盘事实、Skill 路径补全与错误码归属。"""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.assistant_transport.service.user_input_service as user_input_module
from app.assistant_transport.service.user_input_service import UserInputService
from app.models.errors.input_errors import UserInputRejectedError
from app.models.message_context.capsules.grammar.file_capsule import FileCapsule
from app.models.message_context.capsules.grammar.quote_capsule import QuoteCapsule
from app.models.run.run_command import RunCommand
from tests.model_config_fixtures import model_config_record, model_configs

_SESSION_ID = 7


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _service(monkeypatch: pytest.MonkeyPatch, workspace_root: Path) -> UserInputService:
    """不触达存储的 ``UserInputService``：Session 归属 workspace 7，模型不支持图片。"""
    monkeypatch.setattr(
        user_input_module,
        "get_model_config_service",
        lambda: model_configs(lambda _id: model_config_record(model_name="m", context_window_k=8)),
    )
    monkeypatch.setattr(
        user_input_module,
        "get_workspace_service",
        lambda: SimpleNamespace(
            get_workspace=lambda _workspace_id: SimpleNamespace(root_path=str(workspace_root))
        ),
    )
    service = UserInputService.__new__(UserInputService)
    service._session = SimpleNamespace(get=lambda _session_id: SimpleNamespace(workspace_id=7))
    return service


def _prepare(service: UserInputService, text: str):
    return service.prepare(
        _SESSION_ID, RunCommand(text=text), "test/m1", None, "main_agent"
    )


def _write_skill(root: Path, name: str, *, disabled: list[str]) -> None:
    directory = root / ".agents" / "skills" / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\n正文\n", encoding="utf-8"
    )
    config = root / ".cosir" / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "skills.json").write_text(
        '{"disabled_skills": [' + ", ".join(f'"{item}"' for item in disabled) + "]}",
        encoding="utf-8",
    )


# ── file 胶囊 ─────────────────────────────────────────────────────────────


def test_file_capsule_accepts_existing_file(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    path = workspace / "notes.md"
    path.write_text("hi", encoding="utf-8")

    body = _prepare(_service(monkeypatch, workspace), f'看 <file path="{path}" />')

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

    body = _prepare(_service(monkeypatch, workspace), f'看 <file path="{sub}/" />')

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

    text = f"看 {FileCapsule(path=str(path)).serialize()}"
    body = _prepare(_service(monkeypatch, workspace), text)

    assert body.text == f'看 <file path="{workspace}/a &amp; b &quot;q&quot;.md" />'


# ── ref 胶囊 ──────────────────────────────────────────────────────────────


def test_ref_capsule_accepts_workspace_relative_path(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    target = workspace / "src" / "a.py"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")

    body = _prepare(_service(monkeypatch, workspace), '检查 <ref path="src/a.py" lines="12-14" />')

    assert body.text == '检查 <ref path="src/a.py" lines="12-14" />'


def test_ref_capsule_accepts_workspace_root_as_empty_path(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()

    body = _prepare(_service(monkeypatch, workspace), '<ref path="" />')

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
    _write_skill(workspace, "review", disabled=[])
    expected = (workspace / ".agents" / "skills" / "review" / "SKILL.md").resolve()

    body = _prepare(_service(monkeypatch, workspace), '用 <skill name="review" /> 检查')

    assert body.text == f'用 <skill name="review" path="{expected}" /> 检查'
    assert body.presentation.text == body.text


@pytest.mark.parametrize("name", ["off", "missing"])
def test_unavailable_skill_is_rejected_before_enqueue(
    workspace: Path, monkeypatch, name: str
) -> None:
    workspace.mkdir()
    _write_skill(workspace, "off", disabled=["off"])

    with pytest.raises(UserInputRejectedError) as caught:
        _prepare(_service(monkeypatch, workspace), f'用 <skill name="{name}" />')

    assert caught.value.code == "SKILL_UNAVAILABLE"


# ── 语法与空输入 ──────────────────────────────────────────────────────────


def test_code_block_capsules_stay_literal_text(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    text = '```\n<file path="/missing" />\n<ref path="../outside" />\n```'

    body = _prepare(_service(monkeypatch, workspace), text)

    assert body.text == text


def test_invalid_capsule_syntax_stays_literal_text(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    text = '<file path="/missing"> 与 <ref path="a" lines="x" />'

    body = _prepare(_service(monkeypatch, workspace), text)

    assert body.text == text


def test_capsule_only_input_is_not_empty(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    path = workspace / "notes.md"
    path.write_text("hi", encoding="utf-8")

    body = _prepare(_service(monkeypatch, workspace), f'<file path="{path}" />')

    assert body.text == f'<file path="{path}" />'


# ── quote 摘录 ────────────────────────────────────────────────────────────


def test_quote_text_is_not_validated_as_pointers(workspace: Path, monkeypatch) -> None:
    """摘录原文里的 ``<file>``、围栏与反引号是原话，不是用户附的指针：不校验、不切断标签。"""
    workspace.mkdir()
    quote = QuoteCapsule(text='看 <file path="/missing" />\n```\nx`y`\n```')

    body = _prepare(_service(monkeypatch, workspace), f"{quote.serialize()} 改一下")

    assert body.text == f"{quote.serialize()} 改一下"


def test_quote_over_limit_is_rejected(workspace: Path, monkeypatch) -> None:
    workspace.mkdir()
    quote = QuoteCapsule(text="字" * (QuoteCapsule.MAX_TEXT_LENGTH + 1))

    with pytest.raises(UserInputRejectedError) as error:
        _prepare(_service(monkeypatch, workspace), quote.serialize())

    assert error.value.code == "CAPSULE_QUOTE_TOO_LONG"


def test_model_sees_quote_as_block_with_original_text(workspace: Path, monkeypatch) -> None:
    """模型看到展开的 ``<quote>`` 块与原文、批注紧跟其后；转义实体只留在展示正文里。"""
    workspace.mkdir()
    quote = QuoteCapsule(text="第一行\n`code` </quote> 结尾", note="这里为什么")
    body = _prepare(_service(monkeypatch, workspace), f"{quote.serialize()}\n整体再看看")

    content, _metadata = body.render()

    assert isinstance(content["content"], str)
    assert "<quote>\n第一行\n`code` &lt;/quote> 结尾\n</quote>\n这里为什么\n\n整体再看看" in content["content"]
    assert "&#10;" not in content["content"]


def test_file_quote_checks_path_and_shows_source_to_model(workspace: Path, monkeypatch) -> None:
    """来自文件的摘录按 ``ref`` 同样校验路径；模型看到 ``source``，能按位置读上下文。"""
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    quote = QuoteCapsule(text="def a():", path="src/a.py", lines="1-2", note="为什么")

    content, _metadata = _prepare(_service(monkeypatch, workspace), quote.serialize()).render()

    assert '<quote source="src/a.py:1-2">\ndef a():\n</quote>\n为什么' in str(content["content"])
    with pytest.raises(UserInputRejectedError):
        _prepare(_service(monkeypatch, workspace), QuoteCapsule(text="x", path="missing.py").serialize())
