"""图片路径判定与视觉落盘目录归属（leaf 层纯工具）。

单一职责：承载"路径是否为图片"与"路径是否落在 workspace 受信 ``.cosir`` 目录内"
两类**纯路径判断**逻辑，不触碰文件系统读取、不依赖模型能力、不依赖任何业务模型。

为什么放在 utils（leaf）而非 vision_content_blocks：
- 这两类判断被多处复用：附件类型推断、运行期视觉预筛
  （``core/workflows/.../vision_content_blocks`` 编排层）、workflow 图片路径过滤。
  放在编排层会导致其它层跨层引用其私有函数，违反分层依赖。
- 它们只依赖标准库 ``mimetypes`` 与同属 ``app.utils`` 的 ``constants`` / ``cosir_paths``，
  无循环依赖风险，符合 leaf 层定位。

不负责：图片真实格式/尺寸/体积校验（属运行期 Pillow 校验，在 vision_content_blocks）、
图片 base64 编码（同处）、模型视觉能力查询（属 llm_provider 层）。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from app.utils.constants import IMAGE_EXTENSIONS
from app.utils.cosir_paths import is_within_cosir


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


def is_trusted_cosir_path(image_path: str, workspace_root: str | None) -> bool:
    """判断图片路径是否落在受信的 workspace ``.cosir`` 目录内（抗符号链接/大小写）。

    视觉路径不复用写工具 ``PathResolver`` 的 workspace 边界硬拒；此处单独做受信前缀判断，
    仅用于决定是否需要按"外部路径"做额外保守校验（两类路径最终都走同样的读/校验逻辑）。

    参数:
        image_path: 用户图片路径。
        workspace_root: workspace 根路径（用于推导 ``.cosir`` 目录）；为空/非法时按外部路径处理。

    返回:
        ``True`` 表示路径落在 ``.cosir`` 受信目录内（含子目录）；否则 ``False``。

    异常:
        无：底层 :func:`app.utils.cosir_paths.is_within_cosir` 将 ``OSError`` 归一化为 ``False``。

    副作用:
        无。

    说明:
        判定委托 :func:`app.utils.cosir_paths.is_within_cosir`，使 ``.cosir`` 归属规则保持
        单一来源，避免本模块与其它调用点各自实现而漂移。
    """

    return is_within_cosir(image_path, workspace_root)
