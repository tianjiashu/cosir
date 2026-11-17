"""后端等宿主 ``browser.*`` 方法的时限：宿主方法总时限的镜像，加上链路与工具收尾的余量。"""

from __future__ import annotations

from typing import Final

# 宿主给每个方法的总时限（秒），宿主保证在这之内答复（超时也答复，码为 ``DEADLINE_EXCEEDED``）。
# 唯一来源是宿主的 ``contract/method-deadlines.json``；这里是镜像（后端运行时不读 desktop 目录），
# 由 ``tests/test_browser_contract_guard.py`` 核对。后端不按宿主内部各步的超时推算最坏耗时。
HOST_DEADLINES: Final[dict[str, float]] = {
    "browser.act": 330,
    "browser.evaluate": 20,
    "browser.logs": 5,
    "browser.navigate": 60,
    "browser.screenshot": 30,
    "browser.snapshot": 30,
    "browser.tabs": 5,
    "browser.sessionIds": 5,
}
# 链路往返的余量：后端比宿主多等这么多，宿主的答复（包括超时答复）总能先到，模型拿得到做到了哪一步。
_LINK_MARGIN_SECONDS = 5.0
# 工具线程自己的收尾（如截图存成附件）。
_TOOL_MARGIN_SECONDS = 10.0


def host_timeout(method: str) -> float:
    """后端等一次宿主调用的时限。"""

    return HOST_DEADLINES[method] + _LINK_MARGIN_SECONDS


def tool_timeout(*methods: str) -> float:
    """工具的执行时限：一次执行里依次调用的这些方法的等待时限之和，再加工具自己收尾的余量。"""

    return sum(host_timeout(method) for method in methods) + _TOOL_MARGIN_SECONDS
