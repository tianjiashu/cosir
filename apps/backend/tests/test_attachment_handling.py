"""附件处理链路单元测试（仅后端）。

覆盖：
- ``AttachmentRef.from_ref`` 类型推断（url / image / file / 显式 kind）。
- ``render_attachment_refs_to_text`` 把文件/目录/链接渲染为模型可读文本前缀。
- ``ConversationRunService.create_run``：非图片附件拼进落库 ``input_text``，图片单独落
  ``image_paths``；视觉拦截按 kind 判定。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from unittest import mock

from app.models.attachment_ref import AttachmentRef
from app.models.errors.llm_provider_exceptions import VisionNotSupportedError
from app.utils.file_utils import render_attachment_refs_to_text


def test_from_ref_url_explicit_and_inferred() -> None:
    """http(s) 链接无论是否显式指定都判定为 url。"""
    assert AttachmentRef.from_ref("https://example.com/a").kind == "url"
    assert AttachmentRef.from_ref("http://example.com", kind="url").kind == "url"


def test_from_ref_image_by_extension() -> None:
    """图片扩展名自动推断为 image；显式 kind 优先。"""
    assert AttachmentRef.from_ref("/x/y.png").kind == "image"
    assert AttachmentRef.from_ref("/x/y.JPEG").kind == "image"
    assert AttachmentRef.from_ref("/x/y.gif").kind == "image"
    assert AttachmentRef.from_ref("/x/y.webp").kind == "image"
    # 显式覆盖：即便带图片后缀，显式 file 优先
    assert AttachmentRef.from_ref("/x/y.png", kind="file").kind == "file"


def test_from_ref_plain_path_defaults_to_file() -> None:
    """非 url、非图片后缀的纯路径默认为 file（目录/文件真实判定在 service 层）。"""
    assert AttachmentRef.from_ref("/src/main.py").kind == "file"
    assert AttachmentRef.from_ref("/src").kind == "file"


def test_from_ref_rejects_blank() -> None:
    """空引用应抛 ValueError。"""
    try:
        AttachmentRef.from_ref("   ")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_render_mixed_attachments() -> None:
    """文件/目录/链接混合渲染为带标注的文本块，图片被排除。"""
    atts = [
        AttachmentRef(kind="file", ref="/a/b.py"),
        AttachmentRef(kind="directory", ref="/src"),
        AttachmentRef(kind="url", ref="https://example.com/doc"),
        AttachmentRef(kind="image", ref="/pic.png"),
    ]
    text = render_attachment_refs_to_text(atts)
    assert "[附件参考]" in text
    assert "- 文件: /a/b.py" in text
    assert "- 目录: /src" in text and "list_directory" in text
    assert "- 链接: https://example.com/doc" in text and "web_extract" in text
    # 图片不进入文本
    assert "/pic.png" not in text


def test_render_empty_returns_blank() -> None:
    """空附件列表返回空字符串，调用方据此决定是否拼接。"""
    assert render_attachment_refs_to_text([]) == ""


def _load_conversation_run_state_service() -> object:
    """以 importlib 加载 conversation_run_state_service，注入循环依赖 mock，避免顶层 import 触发循环。"""
    sys.modules.setdefault("app.api.dependencies", mock.MagicMock())
    sys.modules.setdefault("app.service.conversation_run_message_store", mock.MagicMock())
    ts_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "service", "task", "conversation_run_service.py"
    )
    spec = importlib.util.spec_from_file_location(
        "app.service.task.conversation_run_state_service_test", os.path.abspath(ts_path)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.service.task.conversation_run_state_service_test"] = module
    spec.loader.exec_module(module)
    return module


def test_create_run_renders_non_image_into_input_text_and_stores_image_paths() -> None:
    """非图片附件拼进落库 input_text；图片单独落 image_paths；视觉拦截按 kind。"""
    ts = _load_conversation_run_state_service()
    image_path = os.path.join(tempfile.gettempdir(), "x.png")

    provider = mock.MagicMock()
    provider.name = "deepseek"
    provider_cap = mock.MagicMock()
    provider_cap.models = ("deepseek-v4-flash-vision-exp",)
    model_cap = mock.MagicMock()
    model_cap.supports_image = True

    with (
        mock.patch.object(ts, "get_provider_service") as mock_gps,
        mock.patch.object(ts, "service_depends") as _svc_dep,
        mock.patch.object(ts.ModelCapability, "get_capability", return_value=model_cap),
        mock.patch.object(ts.ProviderCapability, "get_capability", return_value=provider_cap),
    ):
        mock_gps.return_value.get_provider.return_value = provider
        created = mock.MagicMock()
        created.input_text = None
        created.image_paths = None
        _svc_dep.get_conversation_run_crud.return_value.create.return_value = created

        svc = ts.ConversationRunService()
        svc.create_run(
            task_id=1,
            input_text="请处理",
            provider_id=1,
            model_name="deepseek-v4-flash-vision-exp",
            attachments=[
                ts.AttachmentRef(kind="file", ref="/a/b.py"),
                ts.AttachmentRef(kind="directory", ref="/src"),
                ts.AttachmentRef(kind="url", ref="https://example.com/doc"),
                ts.AttachmentRef(kind="image", ref=image_path),
            ],
        )
        # 落库 create 收到的参数（conversation_run_crud.create 签名：
        # self, task_id, input_text, status, agent_id=, provider_id=, model_name=,
        # image_paths=, reasoning_effort=；input_text 为位置第 2 个，image_paths 为关键字）
        call_args = _svc_dep.get_conversation_run_crud.return_value.create.call_args
        stored_input_text = call_args.args[1]
        stored_image_paths = call_args.kwargs["image_paths"]
        assert stored_image_paths == [image_path]
        assert "[附件参考]" in stored_input_text
        assert "- 文件: /a/b.py" in stored_input_text
        assert "- 目录: /src" in stored_input_text
        assert "- 链接: https://example.com/doc" in stored_input_text


def test_create_run_no_attachments_keeps_input_text_untouched() -> None:
    """无附件时 input_text 不被拼接、image_paths 为 None。"""
    ts = _load_conversation_run_state_service()
    provider = mock.MagicMock()
    provider.name = "deepseek"
    provider_cap = mock.MagicMock()
    provider_cap.models = ("deepseek-v4-flash-vision-exp",)
    model_cap = mock.MagicMock()
    model_cap.supports_image = True

    with (
        mock.patch.object(ts, "get_provider_service") as mock_gps,
        mock.patch.object(ts, "service_depends") as _svc_dep,
        mock.patch.object(ts.ModelCapability, "get_capability", return_value=model_cap),
        mock.patch.object(ts.ProviderCapability, "get_capability", return_value=provider_cap),
    ):
        mock_gps.return_value.get_provider.return_value = provider
        created = mock.MagicMock()
        _svc_dep.get_conversation_run_crud.return_value.create.return_value = created

        svc = ts.ConversationRunService()
        svc.create_run(
            task_id=1,
            input_text="纯文本",
            provider_id=1,
            model_name="deepseek-v4-flash-vision-exp",
        )
        call_args = _svc_dep.get_conversation_run_crud.return_value.create.call_args
        assert call_args.args[1] == "纯文本"
        assert call_args.kwargs["image_paths"] is None


def test_create_run_image_without_vision_raises() -> None:
    """携带图片但模型不支持视觉 -> VisionNotSupportedError（按 kind 判定）。"""
    ts = _load_conversation_run_state_service()
    image_path = os.path.join(tempfile.gettempdir(), "x.png")
    provider = mock.MagicMock()
    provider.name = "deepseek"
    provider_cap = mock.MagicMock()
    provider_cap.models = ("deepseek-v4-flash",)
    model_cap = mock.MagicMock()
    model_cap.supports_image = False

    with (
        mock.patch.object(ts, "get_provider_service") as mock_gps,
        mock.patch.object(ts, "service_depends") as _svc_dep,
        mock.patch.object(ts.ModelCapability, "get_capability", return_value=model_cap),
        mock.patch.object(ts.ProviderCapability, "get_capability", return_value=provider_cap),
    ):
        mock_gps.return_value.get_provider.return_value = provider
        svc = ts.ConversationRunService()
        try:
            svc.create_run(
                task_id=1,
                input_text="看图",
                provider_id=1,
                model_name="deepseek-v4-flash",
                attachments=[ts.AttachmentRef(kind="image", ref=image_path)],
            )
            raise AssertionError("expected VisionNotSupportedError")
        except VisionNotSupportedError:
            pass


def test_create_run_request_rejects_url_without_http_scheme() -> None:
    """CreateTurnRequest：url 类型附件缺少 http(s):// 前缀应被结构校验拒绝。"""
    from pydantic import ValidationError

    from app.api.schemas.request.CreateTurnRequest import CreateTurnRequest

    try:
        CreateTurnRequest(
            input_text="参考",
            attachments=[AttachmentRef(kind="url", ref="ftp://example.com/x")],
        )
        raise AssertionError("expected ValidationError")
    except ValidationError:
        pass


def test_create_run_request_rejects_over_max_attachments() -> None:
    """CreateTurnRequest：附件数超过 20 上限应被结构校验拒绝。"""
    from pydantic import ValidationError

    from app.api.schemas.request.CreateTurnRequest import CreateTurnRequest

    try:
        CreateTurnRequest(
            input_text="参考",
            attachments=[AttachmentRef(kind="file", ref=f"/f{i}") for i in range(21)],
        )
        raise AssertionError("expected ValidationError")
    except ValidationError:
        pass


def test_create_run_request_rejects_blank_ref() -> None:
    """CreateTurnRequest：附件 ref 为空白应被结构校验拒绝。"""
    from pydantic import ValidationError

    from app.api.schemas.request.CreateTurnRequest import CreateTurnRequest

    try:
        CreateTurnRequest(
            input_text="参考",
            attachments=[AttachmentRef(kind="file", ref="   ")],
        )
        raise AssertionError("expected ValidationError")
    except ValidationError:
        pass


def test_create_run_request_accepts_valid_mixed_attachments() -> None:
    """CreateTurnRequest：混合合法附件应通过结构校验。"""
    from app.api.schemas.request.CreateTurnRequest import CreateTurnRequest

    req = CreateTurnRequest(
        input_text="参考",
        attachments=[
            AttachmentRef(kind="file", ref="/a/b.py"),
            AttachmentRef(kind="directory", ref="/src"),
            AttachmentRef(kind="url", ref="https://example.com/doc"),
            AttachmentRef(kind="image", ref="/pic.png"),
        ],
    )
    assert req.attachments is not None
    assert len(req.attachments) == 4
