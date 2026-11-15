"""MCP 服务配置里的环境变量引用：语法与替换规则的唯一实现。

写法与 Claude Code 的 ``.mcp.json`` 一致：``${VAR}``，``${VAR:-默认值}``（变量未设置或为空时用默认值）；
``${env:VAR}`` 是 Cursor 的写法，作为同一种引用的别名接受，复制来的配置不用改。

值从后端进程环境取：启动时已合并用户登录 shell 的环境，系统 ``.cosir/.env`` 的键也由 ``Settings`` 注入，
所以用户在 shell 里 export 的、在配置页就地填写（写进系统 ``.env``）的都能被引用。宿主契约变量
（``Constant.HostEnvironment.VARIABLE_PREFIX``）不许引用：工作区配置随仓库共享，否则仓库作者能把宿主
token 拼进 url 发到任意服务。

本模块只处理字符串；哪些字段允许引用由 ``MCPServerConfig`` 决定。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.config.constant import Constant

_REFERENCE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class VariableReference:
    """一处引用：变量名与是否带默认值。"""

    name: str
    has_default: bool


class MissingVariablesError(ValueError):
    """引用的变量没有设置、也没有默认值；连接因此无法建立。"""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__(f"MCP 服务引用的环境变量未设置：{', '.join(names)}")


def find_references(text: str) -> list[VariableReference]:
    return [
        VariableReference(match.group(1), match.group(2) is not None)
        for match in _REFERENCE.finditer(text)
    ]


def has_reference(text: str) -> bool:
    return _REFERENCE.search(text) is not None


def is_reserved(name: str) -> bool:
    return name.startswith(Constant.HostEnvironment.VARIABLE_PREFIX)


def substitute(text: str, environ: Mapping[str, str], missing: list[str]) -> str:
    """替换文本里的全部引用；缺少的变量名追加进 ``missing``（由调用方汇总后一次报错），原样保留引用。"""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        value = environ.get(name, "")
        if value:
            return value
        if default is not None:
            return default
        if name not in missing:
            missing.append(name)
        return match.group(0)

    return _REFERENCE.sub(replace, text)
