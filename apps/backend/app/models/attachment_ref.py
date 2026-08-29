"""用户轮次附件引用值对象。

单一职责：承载用户输入中携带的附件引用（图片 / 文件 / 目录 / 链接），
以结构化形式区分类型，避免下游靠文件后缀或前缀猜测语义。
不负责文件系统访问、workspace 边界判定或业务校验（那些在 service 层）。
"""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from app.utils.image_utils import is_image_path

# 附件类型枚举：图片走多模态通道，文件/目录/链接拼进 input_text 由模型用工具读取。
AttachmentKind = Literal["image", "file", "directory", "url"]

_URL_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class AttachmentRef:
    """用户轮次附件的结构化引用。

    属性:
        kind: 附件类型（``image`` / ``file`` / ``directory`` / ``url``）。
        ref: 附件引用本身——本地路径（文件/目录/图片）或 http(s) 链接。
    """

    kind: AttachmentKind
    ref: str

    @staticmethod
    def _is_url(ref: str) -> bool:
        """判定引用是否为 http(s) 链接。

        参数:
            ref: 待判定的原始引用字符串。

        返回:
            引用以 http/https 方案开头时返回 True；否则返回 False。

        副作用:
            无。
        """
        return urlparse(ref).scheme in _URL_SCHEMES

    @classmethod
    def from_ref(cls, ref: str, *, kind: AttachmentKind | None = None) -> "AttachmentRef":
        """从单个引用字符串构造附件对象。

        调用方若能明确类型（如前端已分类）可经 ``kind`` 直接指定；否则按以下
        顺序推断：http(s) 链接 → ``url``；图片扩展名 → ``image``；
        其余纯路径默认按 ``file`` 处理（目录/文件的真实判定需在 service 层
        访问文件系统，本 leaf 层不碰 IO）。

        参数:
            ref: 原始引用（路径或链接）。
            kind: 可选显式类型；为 None 时自动推断。

        返回:
            构造好的 ``AttachmentRef``。

        异常:
            ValueError: 当 ``ref`` 为空或全空白时抛出。

        副作用:
            无。
        """
        if not ref or not ref.strip():
            raise ValueError("attachment ref must not be blank")
        if kind is not None:
            return cls(kind=kind, ref=ref.strip())
        if cls._is_url(ref):
            return cls(kind="url", ref=ref.strip())
        if is_image_path(ref):
            return cls(kind="image", ref=ref.strip())
        return cls(kind="file", ref=ref.strip())
