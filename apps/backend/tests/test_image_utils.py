"""图片路径判定与 cosir 受信归属的纯函数单元测试（仅后端）。

覆盖：
- ``app.utils.image_utils.is_image_path``：优先标准库 ``mimetypes`` 判图片，回退业务白名单；
  空/无扩展名/非图片后缀返 False。
- ``app.utils.image_utils.is_trusted_cosir_path``：路径是否落在 workspace ``.cosir`` 受信目录内。
- 复用一致性：``AttachmentRef.from_ref`` 的图片推断与 ``is_image_path`` 行为保持一致。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.models.attachment_ref import AttachmentRef
from app.utils.image_utils import is_image_path, is_trusted_cosir_path


def test_is_image_path_prefers_stdlib_mimetypes_and_falls_back() -> None:
    """优先标准库 mimetypes 判图片；回退业务白名单；无扩展名/非图片后缀/空串返 False。"""
    # 标准库 mimetypes 能识别的常见图片扩展名（小写/大写）
    assert is_image_path("/x/y.png")
    assert is_image_path("/x/y.JPEG")
    assert is_image_path("/x/y.gif")
    assert is_image_path("/x/y.webp")
    # .bmp 在部分平台 mimetypes 可能漏判，但业务白名单兜底仍返回 True（视觉通道早 fail 语义）
    assert is_image_path("/x/y.bmp")
    # 非图片后缀
    assert not is_image_path("/src/main.py")
    # 无扩展名或空/空白
    assert not is_image_path("/noext")
    assert not is_image_path("")
    assert not is_image_path("   ")
    # 纯字符串视角：URL 带图片后缀仍判为 True（URL 语义归属由 attachment_ref._is_url 先拦截）
    assert is_image_path("https://example.com/a.png")


def test_is_trusted_cosir_path_inside_and_outside() -> None:
    """落在 .cosir 内（含子目录）判为受信；workspace 外或空 root 判为不受信。"""
    with tempfile.TemporaryDirectory() as tmp:
        ws = tmp
        cosir = Path(ws) / ".cosir"
        cosir.mkdir()
        inside = cosir / "shot.png"
        sub = cosir / "sub" / "a.jpg"
        sub.parent.mkdir()
        outside = Path(ws) / "other" / "b.png"
        outside.parent.mkdir()

        assert is_trusted_cosir_path(str(inside), ws)
        assert is_trusted_cosir_path(str(sub), ws)
        assert not is_trusted_cosir_path(str(outside), ws)
        # workspace_root 为空时全部按外部路径处理
        assert not is_trusted_cosir_path(str(inside), None)
        assert not is_trusted_cosir_path(str(inside), "")


def test_attachment_ref_image_inference_consistent_with_is_image_path() -> None:
    """from_ref 的图片推断复用 is_image_path，两者对同批样本结论一致。"""
    samples = ["/a/b.png", "/a/b.jpeg", "/src/main.py", "/data", "https://x.com/y"]
    for ref in samples:
        expected_image = is_image_path(ref)
        actual = AttachmentRef.from_ref(ref)
        assert (actual.kind == "image") is expected_image
