"""锁定 Node 运行时解析：从项目内固定目录解析 node，缺失即结构化报错。

单一职责：把「Kernel 该用哪个 node 可执行文件」这一决策收敛到唯一函数
``resolve_node_binary()``，复制桌面端 ``runtime_locator.rs`` 的「从固定目录解析、
绝不读用户 PATH、缺失即明确报错」范式（第一阶段锁定路径，后续接入 Tauri
``resourcesDir`` 时仅改此一处）。

设计边界：
- 不读系统 PATH（避免环境差异导致 Kernel 用错 Node 版本，违背锁定 22 LTS 的初衷）；
- 允许通过 ``CODING_AGENT_CODEGRAPH_NODE`` 环境变量显式覆盖（仅用于第一阶段
  本地验证，指向已满足 ``engines >=20 <25`` 的 node 可执行文件）；
- 固定目录缺失且无显式覆盖时抛 ``CodeGraphNodeMissingError``，给出安装引导。
"""

import os
from pathlib import Path

from app.codegraph.exceptions import CodeGraphNodeMissingError
from app.config.logging.logger import log
from app.config.settings import Settings

#: 第一阶段占位：锁定的 node 位于后端固定资源目录（最终阶段改为桌面 resourcesDir）。
NODE_DIR_ENV = "CODING_AGENT_CODEGRAPH_NODE"


def _fixed_node_candidates() -> list[Path]:
    """推导固定目录下的 node 候选路径（按平台扩展名）。

    参数:
        无。

    返回:
        node 可执行文件路径候选列表（Windows 优先 .exe，POSIX 无扩展名）。

    异常:
        无。

    副作用:
        无。
    """
    backend_root = Settings.repository_root() / "apps" / "backend"
    node_dir = backend_root / ".workspace_payload-node"
    if os.name == "nt":
        return [node_dir / "node.exe"]
    return [node_dir / "node", node_dir / "bin" / "node"]


def resolve_node_binary() -> Path:
    """解析 Kernel 使用的 node 可执行文件绝对路径。

    解析顺序：
        1. 环境变量 ``CODING_AGENT_CODEGRAPH_NODE``（显式覆盖，仅验证用）；
        2. 项目内固定目录 ``apps/backend/.workspace_payload-node/node[.exe]``；
        3. 以上均无 → 抛 ``CodeGraphNodeMissingError``（绝不回退到 PATH）。

    参数:
        无。

    返回:
        node 可执行文件的绝对路径。

    异常:
        CodeGraphNodeMissingError: 当固定目录与环境变量均未提供有效 node 时抛出，
            消息含安装引导。

    副作用:
        无；仅做存在性检查与日志。
    """
    override = os.environ.get(NODE_DIR_ENV)
    if override:
        candidate = Path(override).resolve()
        if not candidate.is_file():
            raise CodeGraphNodeMissingError(
                f"CODEGRAPH node override not found at {candidate} "
                f"(set by {NODE_DIR_ENV}); point it at a node >=20 <25 executable."
            )
        log.info(
            "codegraph_node_resolved",
            extra={"msg": "使用显式覆盖的 node 路径（验证用）", "data": {"path": str(candidate)}},
        )
        return candidate

    for candidate in _fixed_node_candidates():
        if candidate.is_file():
            log.info(
                "codegraph_node_resolved",
                extra={"msg": "从固定目录解析到锁定 node", "data": {"path": str(candidate)}},
            )
            return candidate

    fixed = _fixed_node_candidates()[0]
    raise CodeGraphNodeMissingError(
        f"locked node not found at {fixed}; place a node >=20 <25 binary there "
        f"(or set {NODE_DIR_ENV} for local verification). The Kernel must use a "
        f"pinned node, never one resolved from PATH."
    )
