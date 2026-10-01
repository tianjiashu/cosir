"""workspace 配置的受信任上下文解析。

本模块只把数据库中的 ``workspace_id`` 解析为已登记的 workspace 根路径及固定配置目录，不读写
配置文件，也不承载具体配置格式。所有 workspace 配置 API 复用这里的路径边界，避免客户端传入
任意文件路径绕过 workspace 事实源。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.models import WorkspaceRecord
from app.service import depends as service_depends
from app.utils.path.workspace_cosir import workspace_agent_config_dir, workspace_cosir_dir


@dataclass(frozen=True)
class WorkspaceConfigurationContext:
    """一个已登记 workspace 的配置路径上下文。

    该值对象不缓存配置内容；内容和运行时 profile 分别由各配置 service 管理。
    """

    workspace: WorkspaceRecord
    root: Path
    cosir_root: Path
    agent_directory: Path
    fileignore_path: Path


def get_workspace_configuration_context(workspace_id: int) -> WorkspaceConfigurationContext:
    """根据 workspace ID 构造配置上下文。

    参数:
        workspace_id: 已登记 workspace 的数据库标识。

    返回:
        包含规范化 workspace 根路径及三类配置固定路径的上下文。

    异常:
        KeyError: workspace 不存在。
        ValueError: 数据库中的根路径不是绝对路径或已不再是目录。

    副作用:
        读取一次 workspace 数据库记录并检查根目录；不创建目录、不读写配置文件。
    """

    workspace = service_depends.get_workspace_service().get_workspace(workspace_id)
    root = Path(workspace.root_path).expanduser()
    if not root.is_absolute() or not root.exists() or not root.is_dir():
        raise ValueError("workspace root_path must be an existing absolute directory")
    root = root.resolve()
    cosir_root = workspace_cosir_dir(root)
    return WorkspaceConfigurationContext(
        workspace=workspace,
        root=root,
        cosir_root=cosir_root,
        agent_directory=workspace_agent_config_dir(root),
        fileignore_path=cosir_root / ".fileignore",
    )
