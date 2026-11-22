"""交付记录：Agent 用 ``present_deliverables`` 声明的一项成果（文件、目录或网址）。

挂在工具结果行的 metadata 上（``ToolResultFacts.deliverables``），是后端持久化事实，随 fork、撤回、
删除跟随；工具 part 上的 ``deliverables`` 与 snapshot 顶层清单都是它的投影。展示数据（``display_data``）
按契约只给前端，后端要读的事实不放那里。

字段都是交付那一刻的观察：``kind``、``size`` 与位图的 ``width`` / ``height``（按 EXIF 转正后）由 ``inspect_file``
得出，用于排版——卡片的排版类别与占位尺寸只看记录，渲染与估高不必等现读；工作区内文件的现状
（大小、是否还在）由前端现读，不从这里取。名字是 ``deliverables`` 而不是 ``delivery``：后者在仓里已经指
消息的投递时机（``SessionMessage.delivery``）。
"""

import ipaddress
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums.file_kind import FileKind

_DEFAULT_PORTS = {"http": 80, "https": 443}
# WHATWG 的 forbidden host code points（主机里出现即解析失败）；控制字符与空格另行判断。
_FORBIDDEN_HOST_CHARS = frozenset("#%/:<>?@[\\]^|")


class DeliveredFile(BaseModel):
    """一个已存在的普通文件。``absolute_path`` 是规范路径（``canonical_path``），也是判等值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["file"] = "file"
    # 在工作区内时是相对工作区根的 POSIX 路径，否则为空；前端据它区分内外，不解析盘符或反斜杠。
    workspace_path: str | None
    absolute_path: str
    caption: str | None
    kind: FileKind
    size: int
    # 位图的宽高；读不出或不是位图（含 SVG）时为空。
    width: int | None
    height: int | None

    @property
    def key(self) -> str:
        return self.absolute_path


class DeliveredDirectory(BaseModel):
    """一个已存在的目录。目录不经 ``inspect_file``，也不由前端现读，点击时由打开链路确认。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["directory"] = "directory"
    workspace_path: str | None
    absolute_path: str
    caption: str | None

    @property
    def key(self) -> str:
        return self.absolute_path


class DeliveredUrl(BaseModel):
    """一个 http(s) 网址。「什么是可交付的网址」只在这里判定，构造即校验：协议只认 http / https；不带用户名密码
    （交付记录会持久化、显示、随 fork 复制，凭据不该进去）；主机非空、端口合法，主机不含浏览器拒绝的字符。

    规则取浏览器（WHATWG URL）的一个子集：前端直接用 ``new URL`` 解析这里存下的地址，后端收下的必须是浏览器
    解析得了的，否则一个坏地址会让整组卡片渲染失败。标准库没有 WHATWG 实现，所以手写主机这一段的检查，前后端
    用同一份样本表守住（``tests/fixtures/deliverable_urls.json``）。不联网，不检查是否可达。

    异常:
        ValidationError: 构造时地址不合法；消息是给模型看的原因，工具把它归为这一项失败。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["url"] = "url"
    url: str
    caption: str | None

    @field_validator("url")
    @classmethod
    def _deliverable_address(cls, url: str) -> str:
        try:
            parts = urlsplit(url)
            _ = parts.port  # 端口只在读取时解析，非法端口（越界、非数字）在这里抛出
        except ValueError as exc:
            raise ValueError(f"not a valid web address ({exc})") from exc
        if parts.scheme.lower() not in _DEFAULT_PORTS:
            raise ValueError("only http:// and https:// web addresses can be presented")
        if parts.username is not None or parts.password is not None:
            raise ValueError("remove the user name or password from the address")
        host = parts.hostname or ""
        if not host:
            raise ValueError("not a valid web address (missing host)")
        if ":" in host:
            try:
                ipaddress.IPv6Address(host)
            except ValueError as exc:
                raise ValueError(f"not a valid web address ({exc})") from exc
        elif any(
            char in _FORBIDDEN_HOST_CHARS or ord(char) <= 0x20 or ord(char) == 0x7F for char in host
        ):
            raise ValueError(
                "not a valid web address (the host contains characters browsers reject)"
            )
        return url

    @property
    def key(self) -> str:
        return self.normalized(self.url)

    @staticmethod
    def normalized(url: str) -> str:
        """「同一指向」的网址判等值：协议与主机小写、去掉默认端口、空路径补 ``/``、去掉片段。

        只做这几条：标准库没有 WHATWG 规范化，IDNA 与百分号编码规范化不为此加依赖。``url`` 须已通过构造期校验。
        """

        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        netloc = host if port is None or port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
        return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


Deliverable = Annotated[
    DeliveredFile | DeliveredDirectory | DeliveredUrl, Field(discriminator="type")
]

__all__ = ["Deliverable", "DeliveredDirectory", "DeliveredFile", "DeliveredUrl"]
