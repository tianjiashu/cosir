"""图片路径判定（leaf 层纯工具）：按扩展名判断引用是否为图片，不读取文件、不依赖业务模型。

被附件类型推断与运行期视觉预筛共用，放在 leaf 层避免编排层互相引用私有函数。不负责真实格式、尺寸与
体积校验（运行期 Pillow 校验）和 base64 编码。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from app.utils.constants import IMAGE_EXTENSIONS


def is_image_path(ref: str) -> bool:
    """按扩展名判定引用是否为图片（不信任、不读取真实格式）。

    用于附件分类与运行期视觉预筛两处共用，避免各处重复实现同一份后缀判断。

    判定**优先使用标准库** ``mimetypes``（系统权威映射表，覆盖绝大多数图片扩展名）；
    当 ``mimetypes`` 因运行平台/注册表缺失而判不出（返回 ``None`` 或非 ``image/*``）时，
    回退到业务白名单 ``app.utils.constants.IMAGE_EXTENSIONS``——后者保留 ``.bmp`` 等被
    故意路由到视觉通道、但标准库可能漏判的扩展名，确保视觉通道语义不丢失。

    参数:
        ref: 待判定的引用（本地路径或链接字符串）。

    返回:
        扩展名经 ``mimetypes`` 判为图片，或落在 ``IMAGE_EXTENSIONS`` 业务白名单内返回
        ``True``；否则返回 ``False``。空/非法引用一律返回 ``False``。注意这是**纯字符串**
        判断，不区分本地路径与 URL——``https://x.com/a.png`` 也会返回 ``True``；
    URL 的归属判定由调用方先于本函数完成。

    副作用:
        无。
    """
    if not ref:
        return False
    suffix = Path(ref).suffix.lower()
    if not suffix:
        return False
    # 优先标准库：mimetypes 能将 .png/.jpg/.jpeg/.gif/.webp/.bmp 等映射为 image/*。
    guessed = mimetypes.guess_type(ref)[0]
    if guessed and guessed.startswith("image/"):
        return True
    # 回退业务白名单：覆盖标准库未识别、但本项目视觉通道需要强制路由的扩展名。
    return suffix in IMAGE_EXTENSIONS
