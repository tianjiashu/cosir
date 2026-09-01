"""视觉输入 block 构造（多模态用户消息拼装）。

单一职责：把用户通过 ``turn.image_paths`` 传入的本地图片路径，在**运行期**读文件、做安全/格式
校验、编码为 LangChain ``HumanMessage`` 可用的多模态 block（``image_url`` 形式），并与文本
拼接成完整的内容 block 列表。

设计边界：
- 本模块**不落库**：图片本体与 base64 编码结果均只存在于运行期内存，DB 仅持久化
  ``turn.image_paths`` 字符串（由 ``turn_model`` / ``create_turn`` 负责；文件/目录/url
   附件已固化进 input_text，不走此通道）。
- 本模块**不复用写工具的 ``PathResolver``**：视觉路径分两类契约 —— (a) workspace 内受信的
  ``.cosir`` 目录（前端图片落盘处，后端放心读）；(b) 历史外部路径（兼容旧数据）。两类均不做
  workspace 写边界硬拒，仅做存在性/格式/体积安全校验。
- 逐图失败隔离：单图缺失/非图片/尺寸超限/解码失败 → 进 ``skipped`` 列表 + 分级日志，**不废整轮**。
- 仅有 ``VisionFormatNotSupportedError``（厂商格式未实现）与 ``VisionImageError``（聚合体积超限）
  会作为运行期硬异常抛出，由 workflow 捕获后统一转 ``VisionNotSupportedError``。

Pillow 仅用于运行期读文件校验尺寸/探测真实格式/防解压炸弹，**不参与**序列化（序列化由下游
LangChain 原生透传）。
"""

from __future__ import annotations

import base64
import functools
import io
import os

from PIL import Image

from app.config.logging.logger import log
from app.llm_provider.capability.model_capability import (
    ImageLimitCapability,
    ModelCapability,
)
from app.models.errors.llm_provider_exceptions import (
    VisionFormatNotSupportedError,
    VisionImageError,
)
from app.utils.image_utils import is_image_path, is_trusted_cosir_path

# Pillow 真实格式 -> data URI mime 映射（LangChain / DeepSeek 接受的标准 mime）。
_PILLOW_FORMAT_TO_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

# 未知模型（能力注册表未声明 image_limit）时的保守兜底限制。
# 语义：宁可保守拒图，也不把未登记能力的长尾模型当成「无限宽松」——
# 超限图片走逐图 skipped 隔离，不废整轮，故兜底偏紧是安全的。
_DEFAULT_SINGLE_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MiB（单图内联体积上限）
_DEFAULT_MAX_IMAGE_SIDE_PX = 8000  # 单边像素上限
_DEFAULT_MAX_IMAGE_SIDE_PX_WHEN_MANY = 2000  # 图片数达阈值时的更保守单边上限
# 触发更保守单边上限的图片数阈值。必须 > 0：``many_image_threshold`` 语义是
# 「图片数达到 N 才收紧」，而 0 会让 ``len(paths) >= 0`` 恒真，导致任何图片
# （哪怕只有 1 张）都走 when_many 分支。
_DEFAULT_MANY_IMAGE_THRESHOLD = 8
_DEFAULT_MAX_IMAGES_PER_REQUEST = 8
_DEFAULT_REQUEST_TOTAL_MAX_BYTES = 48 * 1024 * 1024  # 48 MiB（单请求图片总字节上限）

# 兜底值对象（不可变，构造一次复用，避免每次调用都新建）。
_DEFAULT_IMAGE_LIMIT = ImageLimitCapability(
    single_image_inline_max_bytes=_DEFAULT_SINGLE_IMAGE_BYTES,
    max_image_side_px=_DEFAULT_MAX_IMAGE_SIDE_PX,
    max_image_side_px_when_many=_DEFAULT_MAX_IMAGE_SIDE_PX_WHEN_MANY,
    many_image_threshold=_DEFAULT_MANY_IMAGE_THRESHOLD,
    max_images_per_request=_DEFAULT_MAX_IMAGES_PER_REQUEST,
    request_total_max_bytes_inline_only=_DEFAULT_REQUEST_TOTAL_MAX_BYTES,
)


# 防解压炸弹：解码时单图像素上限（超过 Pillow 抛 DecompressionBombError）。
Image.MAX_IMAGE_PIXELS = 200_000_000  # 200 MP


def _resolve_image_limits(model_name: str | None) -> ImageLimitCapability:
    """按模型名解析图片限制，未知模型回退保守默认值。

    未知模型（``model_name`` 为 None，或能力注册表未声明 ``image_limit``）**不报错**，
    而是回退 ``_DEFAULT_IMAGE_LIMIT``：能力注册表只收录了部分模型，若对未收录模型
    直接抛错，长尾模型将完全无法处理图片附件。超限时由调用方走逐图 skipped 隔离
    （仅该图被跳过、整轮继续），因此兜底值偏紧是安全的。

    参数:
        model_name: 当前 turn 使用的模型名；为 ``None`` 或未知模型时回退兜底。

    返回:
        ``ImageLimitCapability`` 实例（来自 ``ModelCapability.image_limit`` 真相源，
        或未知模型时的 ``_DEFAULT_IMAGE_LIMIT`` 兜底）。

    异常:
        无。未知模型不抛异常——抛错会让未登记能力的模型彻底无法处理图片。
    """
    if not model_name:
        return _DEFAULT_IMAGE_LIMIT
    limit = ModelCapability.get_capability(model_name).image_limit
    # 逐字段兜底而非整对象兜底：注册表可能只声明了部分字段（如设了
    # single_image_inline_max_bytes 但 max_image_side_px 留 0）。若因「有任一字段非零」
    # 就整对象采用，未声明字段会以 0 参与比较——典型后果是任何图片都因
    # 「超过 0px」被误拒。归零字段一律视为未声明，用保守默认值补齐。
    #
    # 特别注意 ``many_image_threshold``：其语义是「图片数达到 N 才收紧到
    # when_many 上限」，未声明时为 0，会让 ``len(paths) >= 0`` 恒真，导致
    # 单张图片也走 when_many 分支并读到未声明的 0px 上限。故该字段同样必须兜底。
    return ImageLimitCapability(
        supported_formats=limit.supported_formats,
        single_image_inline_max_bytes=(
            limit.single_image_inline_max_bytes or _DEFAULT_SINGLE_IMAGE_BYTES
        ),
        max_image_side_px=limit.max_image_side_px or _DEFAULT_MAX_IMAGE_SIDE_PX,
        max_image_side_px_when_many=(
            limit.max_image_side_px_when_many or _DEFAULT_MAX_IMAGE_SIDE_PX_WHEN_MANY
        ),
        many_image_threshold=limit.many_image_threshold or _DEFAULT_MANY_IMAGE_THRESHOLD,
        max_images_per_request=limit.max_images_per_request or _DEFAULT_MAX_IMAGES_PER_REQUEST,
        request_total_max_bytes_inline_only=(
            limit.request_total_max_bytes_inline_only or _DEFAULT_REQUEST_TOTAL_MAX_BYTES
        ),
    )


def _is_image_ext(path: str) -> bool:
    """按扩展名初筛是否当作图片尝试读取（不信任其真实格式）。

    委托 ``app.utils.image_utils.is_image_path``（图片扩展名唯一事实源的单一收口），
    避免与附件分类逻辑重复实现。

    参数:
        path: 待判断的文件路径。

    返回:
        扩展名落在白名单（``app.utils.constants.IMAGE_EXTENSIONS``，含
        ``.jpg/.jpeg/.png/.gif/.webp/.bmp``）内返回 ``True``，否则 ``False``。
    """
    return is_image_path(path)


@functools.lru_cache(maxsize=128)
def _cached_encode(path: str, st_size: int, st_mtime: float) -> tuple[str, str]:
    """编码单图为 ``(real_mime, base64_data)``，带 LRU 缓存避免同图跨轮重复读解码。

    缓存键为 ``(路径, 文件大小, 修改时间)``：文件不变则复用，文件变化则失效重编。
    桌面端 Python 后端为单进程，多 task 并发读同一图为只读、无写冲突，``lru_cache`` 纯函数
    共享安全；若未来演进为 multiprocessing 后端，需改为进程级缓存或禁用本缓存。

    参数:
        path: 已通过存在性/可读性校验的图片路径。
        st_size: 文件大小（来自 ``os.stat``，参与缓存键）。
        st_mtime: 修改时间（来自 ``os.stat``，参与缓存键）。

    返回:
        ``(真实 mime, base64 编码串)``。

    异常:
        由调用方 ``_encode_image`` 在外层捕获 Pillow / IO 错误并降级。
    """
    with Image.open(path) as img:
        real_format = (img.format or "JPEG").upper()
        if real_format == "GIF":
            # GIF 仅取首帧；首帧转 PNG 保存（避免多帧编码 + 透明通道差异）
            img = img.convert("RGB")
            save_format = "PNG"
            mime = "image/png"
        else:
            # 保留真实格式与 mime（JPEG/PNG/WEBP），仅对调色板/透明图做 RGB 归一
            if img.mode in ("P", "RGBA", "LA"):
                img = img.convert("RGB")
            save_format = real_format if real_format in _PILLOW_FORMAT_TO_MIME else "JPEG"
            mime = _PILLOW_FORMAT_TO_MIME.get(real_format, "image/jpeg")
        buf = io.BytesIO()
        img.save(buf, format=save_format)
        data = base64.b64encode(buf.getvalue()).decode("ascii")
    return mime, data


def _encode_image_to_block(
    image_path: str,
    vision_input_format: str,
    max_side: int,
    max_single_bytes: int,
) -> dict:
    """将单张图片读+校验+编码为 LangChain 多模态 block。

    参数:
        image_path: 已通过存在性/可读性预检的图片路径。
        vision_input_format: 厂商视觉格式（如 ``"openai_url"``）。
        max_side: 单边像素硬上限（图片数量多时取更保守值）。
        max_single_bytes: 单图体积硬上限（字节），来自 ``ImageLimitCapability`` 真相源。

    返回:
        多模态 block（``{"type": "image_url", ...}``）。

    异常:
        VisionFormatNotSupportedError: 厂商格式本期未实现（非 ``openai_url``）。
        VisionImageError: 尺寸超限 / 解码失败 / 解压炸弹 / 单图体积超限。
    """
    if vision_input_format != "openai_url":
        raise VisionFormatNotSupportedError(
            f"vision input format '{vision_input_format}' is not supported yet"
        )

    try:
        st = os.stat(image_path)
    except OSError as exc:
        raise VisionImageError(f"cannot stat image: {exc}") from exc

    if st.st_size > max_single_bytes:
        raise VisionImageError(f"image too large: {st.st_size} bytes exceeds {max_single_bytes}")

    # 尺寸 + 真实格式 + 解码炸弹防护在 _cached_encode 内的 Image.open 阶段生效
    # （Image.MAX_IMAGE_PIXELS 超限会抛 DecompressionBombError）。
    try:
        mime, data = _cached_encode(image_path, st.st_size, st.st_mtime)
    except Image.DecompressionBombError as exc:
        raise VisionImageError(f"image exceeds decompression bomb limit: {exc}") from exc
    except (OSError, ValueError, Image.UnidentifiedImageError) as exc:
        raise VisionImageError(f"failed to decode image: {exc}") from exc

    # 尺寸校验（统一 RGB 后取尺寸）
    with Image.open(image_path) as probe:
        width, height = probe.size
    if width > max_side or height > max_side:
        raise VisionImageError(f"image dimensions {width}x{height} exceed limit {max_side}px")

    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{data}"},
    }


def build_user_content_blocks(
    text: str,
    image_paths: list[str],
    vision_input_format: str,
    workspace_root: str | None = None,
    model_name: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """构造用户消息的内容 block 列表（文本 + 多模态图片），逐图失败隔离。

    流程（逐图隔离，单图失败不废整轮）：对每个 image_path ->
        路径归属校验（.cosir 受信 vs 历史外部路径，不复用写工具 PathResolver）->
        文件存在 + 可读校验 -> Pillow 打开探测真实格式与尺寸（mime 以真实格式为准，不信任扩展名；
        设 MAX_IMAGE_PIXELS 防解压炸弹）-> 尺寸校验（单边上限；GIF 仅取首帧）-> 编码 data URI ->
        按 vision_input_format 拼 block；坏图收集进 skipped，不抛整轮异常。

    图片限制（单图体积/聚合体积/单边像素/多图阈值）**动态查询** ``ModelCapability.image_limit``
    （来自 ``model_capabilities.json`` 官方文档对齐值），作为唯一事实源；不同模型限制不同，
    后续接入新模型只需在 JSON 声明，无需改此处代码。未知模型回退 ``_DEFAULT_*`` 兜底。

    参数:
        text: 用户文本输入（始终作为首条 text block）。
        image_paths: 经 ``_is_image_ext`` 预筛后的图片路径列表。
        vision_input_format: 厂商视觉格式（如 ``"openai_url"``），由 workflow 从 runtime_config 传入。
        workspace_root: workspace 根路径，用于 ``.cosir`` 受信归属判定；``None`` 时全部按外部路径处理。
        model_name: 当前 turn 使用的模型名，用于动态查询图片限制；``None`` 时回退兜底默认值。

    返回:
        ``(blocks, skipped)`` 二元组：
        - ``blocks``：LangChain 可用的内容 block 列表，首元素为 ``{"type": "text", ...}``，
          后续为图片 block；无图时仍至少含文本 block。
        - ``skipped``：失败图片清单，元素为 ``{"path": str, "reason": str}``，供 workflow 告知用户。

    异常:
        VisionFormatNotSupportedError: 厂商格式未实现，由 workflow 转 ``VisionNotSupportedError``。
        VisionImageError: 聚合体积超过模型限制整轮不可恢复，由 workflow 转 ``VisionNotSupportedError``。

    副作用:
        对失败图片写入分级日志（文件不存在记 info 抑回放噪音；其余记 warning）。
    """
    blocks: list[dict] = [{"type": "text", "text": text}]
    skipped: list[dict] = []

    if not image_paths:
        return blocks, skipped

    limit = _resolve_image_limits(model_name)
    many_images = len(image_paths) >= limit.many_image_threshold
    max_side = limit.max_image_side_px_when_many if many_images else limit.max_image_side_px
    total_bytes = 0
    for image_path in image_paths:
        # .cosir 受信归属校验（仅用于决定是否走受信捷径日志，两类路径均做同样读/校验）
        is_trusted = is_trusted_cosir_path(image_path, workspace_root)

        if not os.path.isfile(image_path) or not os.access(image_path, os.R_OK):
            # 文件不存在/不可读：预期失效（用户已删/移动、跨设备回放），记 info 抑回放噪音
            log.info(
                "vision_image_skipped",
                extra={
                    "msg": "image path not found or unreadable, skipped",
                    "data": {
                        "path": os.path.basename(image_path),  # 仅 basename，脱敏
                        "trusted_cosir": is_trusted,
                        "reason": "not_found_or_unreadable",
                    },
                },
            )
            skipped.append(
                {"path": os.path.basename(image_path), "reason": "not_found_or_unreadable"}
            )
            continue

        try:
            block = _encode_image_to_block(
                image_path, vision_input_format, max_side, limit.single_image_inline_max_bytes
            )
        except VisionImageError as exc:
            # 解码失败/尺寸超限/体积超限：真正异常，记 warning
            log.warning(
                "vision_image_skipped",
                extra={
                    "data": {
                        "path": os.path.basename(image_path),
                        "trusted_cosir": is_trusted,
                        "reason": str(exc),
                    },
                },
            )
            skipped.append({"path": os.path.basename(image_path), "reason": str(exc)})
            continue

        blocks.append(block)
        try:
            total_bytes += os.path.getsize(image_path)
        except OSError:
            pass

    if total_bytes > limit.request_total_max_bytes_inline_only:
        raise VisionImageError(
            f"total image size {total_bytes} bytes exceeds limit "
            f"{limit.request_total_max_bytes_inline_only}"
        )

    return blocks, skipped
