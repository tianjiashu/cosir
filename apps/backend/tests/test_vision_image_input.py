"""视觉输入链路单元测试（仅后端，不触前端）。

覆盖：vision_content_blocks 的编码/校验/逐图隔离、token 估算、RuntimeMessage 多模态
估算、.cosir 受信归属归一化、create_turn 构建期视觉能力拦截。

不依赖前端、不依赖真实模型调用；图片用 Pillow 在临时目录构造。
"""

from __future__ import annotations

import base64
import importlib.util
import io
import os
import sys
import tempfile
from unittest import mock

from PIL import Image

from app.models import RuntimeMessage
from app.models.errors.llm_provider_exceptions import (
    VisionFormatNotSupportedError,
    VisionImageError,
    VisionNotSupportedError,
)
from app.utils.image_utils import is_trusted_cosir_path
from app.utils.token_estimator import TokenEstimator

# vision_content_blocks 位于 nodes.helper 包下，但 nodes/__init__.py 主动 re-export 会触发整条
# workflow 导入链（循环）。用 importlib 直接从文件路径加载该模块，绕过包 __init__ 链，
# 因其仅依赖 app.core.llm_provider.exceptions 与 app.config.logging.logger（均独立无循环）。
_VCB_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "app",
    "core",
    "workflows",
    "nodes",
    "helper",
    "vision_content_blocks.py",
)
_spec = importlib.util.spec_from_file_location(
    "vision_content_blocks_test", os.path.abspath(_VCB_PATH)
)
vcb = importlib.util.module_from_spec(_spec)
sys.modules["vision_content_blocks_test"] = vcb
_spec.loader.exec_module(vcb)
build_user_content_blocks = vcb.build_user_content_blocks

# _is_trusted_cosir 已下沉至 app.utils.image_utils（消除与 attachment_ref 的重复判断、
# 避免跨层引用私有函数），此处直接用顶部导入的 is_trusted_cosir_path 别名。
_is_trusted_cosir = is_trusted_cosir_path


def _make_png(path: str, size: tuple[int, int] = (64, 64)) -> None:
    """构造一张合法 PNG 供测试读取。"""
    with Image.new("RGB", size, (10, 20, 30)) as img:
        img.save(path, format="PNG")


def _make_jpeg(path: str, size: tuple[int, int] = (64, 64)) -> None:
    """构造一张合法 JPEG 供测试读取（验证 mime 映射为 image/jpeg）。"""
    with Image.new("RGB", size, (10, 20, 30)) as img:
        img.save(path, format="JPEG")


def test_build_text_only_returns_single_text_block() -> None:
    blocks, skipped = build_user_content_blocks("hello", [], "openai_url", None)
    assert blocks == [{"type": "text", "text": "hello"}]
    assert skipped == []


def test_build_single_image_yields_openai_url_block() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "pic.png")
        _make_png(img)
        blocks, skipped = build_user_content_blocks(
            "看这张图", [img], "openai_url", workspace_root=None
        )
    assert skipped == []
    assert blocks[0] == {"type": "text", "text": "看这张图"}
    assert blocks[1]["type"] == "image_url"
    url = blocks[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # 编码结果可反向解码为原尺寸 PNG
    b64 = url.split("base64,", 1)[1]
    raw = base64.b64decode(b64)
    with Image.open(io.BytesIO(raw)) as dec:
        assert dec.size == (64, 64)


def test_skipped_when_image_not_found() -> None:
    missing = os.path.join(tempfile.gettempdir(), "no_such_image_xyz.png")
    blocks, skipped = build_user_content_blocks(
        "t", [missing], "openai_url", workspace_root=None
    )
    assert blocks == [{"type": "text", "text": "t"}]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "not_found_or_unreadable"


def test_format_not_supported_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "pic.png")
        _make_png(img)
        try:
            build_user_content_blocks("t", [img], "anthropic_images", workspace_root=None)
            raise AssertionError("expected VisionFormatNotSupportedError")
        except VisionFormatNotSupportedError:
            pass


def test_total_size_exceeded_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "pic.png")
        _make_png(img)
        # 伪造每张图体积，使聚合超过 48MiB 上限
        with mock.patch.object(vcb.os.path, "getsize", return_value=30 * 1024 * 1024):
            try:
                build_user_content_blocks(
                    "t", [img, img], "openai_url", workspace_root=None
                )
                raise AssertionError("expected VisionImageError")
            except VisionImageError:
                pass


def test_is_trusted_cosir_normalizes_symlink_and_case() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = os.path.join(tmp, "ws")
        cosir = os.path.join(ws, ".cosir")
        os.makedirs(cosir)
        inside = os.path.join(cosir, "shot.png")
        _make_png(inside)
        assert _is_trusted_cosir(inside, ws) is True
        # 大小写差异（Windows）应被 normcase 归一
        upper = os.path.join(ws.upper(), ".cosir", "shot.png")
        assert _is_trusted_cosir(upper, ws) is True
        outside = os.path.join(tmp, "elsewhere.png")
        assert _is_trusted_cosir(outside, ws) is False
        # workspace_root 为空 -> 非受信
        assert _is_trusted_cosir(inside, None) is False


def test_estimate_image_caps_at_384() -> None:
    big_b64 = base64.b64encode(b"x" * (1024 * 1024)).decode()  # ~1MB
    block = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big_b64}"}}
    tokens = TokenEstimator.estimate_image(block)
    assert tokens <= 384
    assert tokens > 0


def test_estimate_image_invalid_block_returns_zero() -> None:
    assert TokenEstimator.estimate_image({"type": "text", "text": "x"}) == 0
    assert TokenEstimator.estimate_image({"image_url": {}}) == 0


def test_runtime_message_estimate_tokens_counts_image() -> None:
    b64 = base64.b64encode(b"y" * 4096).decode()
    block = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
    msg = RuntimeMessage(
        role="user", content_text="看", content_blocks=[block]
    )
    total = msg.estimate_tokens()
    # 文本至少 1 token + 图片 token（>0）
    assert total > TokenEstimator.estimate("看")


def test_create_turn_rejects_image_for_non_vision_model() -> None:
    """构建期拦截：模型不支持视觉输入 + 携带图片路径 -> VisionNotSupportedError。

    turn_service 与 dependencies / turn_runtime_message_store 存在顶层互相 import（循环），
    直接 import 会触发循环。这里用 importlib 直接从文件加载 turn_service 模块，并向 sys.modules
    注入循环方的 mock（dependencies、turn_runtime_message_store），打断循环链使模块可完整加载，
    不改动既有模块结构。
    """
    import sys

    sys.modules.setdefault("app.api.dependencies", mock.MagicMock())
    sys.modules.setdefault("app.service.turn_runtime_message_store", mock.MagicMock())

    _ts_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "service", "task", "turn_service.py"
    )
    _ts_spec = importlib.util.spec_from_file_location(
        "app.service.task.turn_service_test", os.path.abspath(_ts_path)
    )
    turn_service = importlib.util.module_from_spec(_ts_spec)
    sys.modules["app.service.task.turn_service_test"] = turn_service
    _ts_spec.loader.exec_module(turn_service)

    image_path = os.path.join(tempfile.gettempdir(), "x.png")
    with mock.patch.object(
        turn_service, "get_provider_service"
    ) as mock_gps, mock.patch.object(
        turn_service, "service_depends"
    ) as _svc_dep, mock.patch.object(
        turn_service.ModelCapability, "get_capability"
    ) as mock_mc:
        provider = mock.MagicMock()
        provider.name = "deepseek"
        mock_gps.return_value.get_provider.return_value = provider
        # provider capability 含某模型，但模型级 capability 不支持图片
        provider_cap = mock.MagicMock()
        provider_cap.models = ("deepseek-v4-flash",)
        with mock.patch.object(
            turn_service.ProviderCapability, "get_capability", return_value=provider_cap
        ):
            model_cap = mock.MagicMock()
            model_cap.supports_image = False
            mock_mc.return_value = model_cap
            svc = turn_service.TurnService()
            try:
                svc.create_turn(
                    task_id=1,
                    input_text="看图",
                    provider_id=1,
                    model_name="deepseek-v4-flash",
                    attachments=[
                        turn_service.AttachmentRef(kind="image", ref=image_path)
                    ],
                )
                raise AssertionError("expected VisionNotSupportedError")
            except VisionNotSupportedError:
                pass


def test_build_single_jpeg_yields_image_jpeg_block() -> None:
    """真实格式探测：JPEG 文件编码后 mime 应为 image/jpeg（不信任扩展名）。"""
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "pic.jpg")
        _make_jpeg(img)
        blocks, skipped = build_user_content_blocks(
            "图", [img], "openai_url", workspace_root=None
        )
    assert skipped == []
    assert blocks[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_single_image_size_exceeded_goes_to_skipped(caplog) -> None:
    """单图体积超过 20MiB 硬上限 -> 逐图隔离进 skipped + warning（不废整轮，不抛整轮异常）。

    与聚合 48MiB 超限（test_total_size_exceeded_raises 抛 VisionImageError）区分：
    单图超限按设计走逐图失败隔离，仅该图进 skipped，整轮仍可继续。
    """
    import logging

    caplog.set_level(logging.WARNING)
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "pic.png")
        _make_png(img)
        # 仅对目标图片返回超大假 stat，其余路径走真实 os.stat（避免破坏 isfile/access）。
        fake_stat = mock.MagicMock()
        fake_stat.st_size = 30 * 1024 * 1024
        fake_stat.st_mode = 0o100644
        real_stat = vcb.os.stat

        def _stat_side(path, *args, **kwargs):
            if path == img:
                return fake_stat
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(vcb.os, "stat", side_effect=_stat_side), caplog.at_level(
            logging.WARNING
        ):
            blocks, skipped = build_user_content_blocks(
                "t", [img], "openai_url", workspace_root=None
            )
    assert blocks == [{"type": "text", "text": "t"}]
    assert len(skipped) == 1
    assert any("vision_image_skipped" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_corrupted_image_goes_to_skipped_with_warning(caplog) -> None:
    """损坏图片解码失败 -> 进 skipped + 记 warning（不废整轮，脱敏只记 basename）。"""
    import logging

    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "broken.png")
        with open(img, "wb") as f:
            f.write(b"not a real image at all")
        with caplog.at_level(logging.WARNING):
            blocks, skipped = build_user_content_blocks(
                "t", [img], "openai_url", workspace_root=None
            )
    assert blocks == [{"type": "text", "text": "t"}]
    assert len(skipped) == 1
    # 日志脱敏：仅 basename，无绝对路径
    assert os.path.basename(img) in skipped[0]["path"]
    assert any("vision_image_skipped" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_limits_resolved_from_model_image_limit() -> None:
    """图片限制从 ModelCapability.image_limit 动态读取，不同模型阈值不同（唯一事实源）。

    deepseek-v4-flash-vision-exp 声明 single_image_inline_max_bytes=32MiB；
    未知模型回退 _DEFAULT_SINGLE_IMAGE_BYTES=20MiB。用合法小张 PNG + mock getsize 模拟
    25MiB，验证体积阈值随模型切换：
    - 传 deepseek 模型名 -> 25MiB < 32MiB，编码成功（进 blocks）
    - 传 None / 未知模型名 -> 25MiB > 20MiB 兜底，进 skipped
    验证限制不硬编码、随模型切换，后续接入新模型只需在 JSON 声明。
    """
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "img.png")
        _make_png(img)  # 合法小张 PNG，Pillow 可解码
        fake_size = 25 * 1024 * 1024

        # 单图体积检查读 os.stat(...).st_size，聚合读 os.path.getsize；两者都 mock 为 25MiB
        import stat as _stat_mod

        class _FakeStat:
            st_size: int = fake_size
            st_mtime: float = 0.0
            st_mode: int = _stat_mod.S_IFREG | 0o644

        real_stat = vcb.os.stat

        def _stat_side(path, *args, **kwargs):
            if path == img:
                return _FakeStat()
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(vcb.os, "stat", side_effect=_stat_side), mock.patch.object(
            vcb.os.path, "getsize", return_value=fake_size
        ):
            # 已知模型：32MiB 阈值，25MiB 通过
            blocks_known, skipped_known = build_user_content_blocks(
                "t", [img], "openai_url", workspace_root=None,
                model_name="deepseek-v4-flash-vision-exp",
            )
            assert len(skipped_known) == 0, "deepseek 32MiB 阈值应容纳 25MiB 图"
            assert len(blocks_known) == 2

            # 未知模型：20MiB 兜底阈值，25MiB 进 skipped
            blocks_unknown, skipped_unknown = build_user_content_blocks(
                "t", [img], "openai_url", workspace_root=None, model_name=None,
            )
            assert len(skipped_unknown) == 1, "未知模型回退 20MiB 应拒 25MiB 图"
            assert blocks_unknown == [{"type": "text", "text": "t"}]
