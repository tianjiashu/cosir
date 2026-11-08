"""workspace 配置的受信任根目录解析。

所有 workspace 配置 API 只接受 ``workspace_id``，由这里解析为已登记的根目录，避免客户端传入
任意文件路径绕过 workspace 事实源。``.cosir`` 下的具体配置路径由各配置 service 经
``app.utils.path`` 取得。
"""

from __future__ import annotations

from pathlib import Path

from app.service import depends as service_depends


def resolve_workspace_root(workspace_id: int) -> Path:
    """返回已登记 workspace 的根目录。

    异常:
        KeyError: workspace 不存在。
        ValueError: 登记的根路径已不再是目录。
    """

    workspace = service_depends.get_workspace_service().get_workspace(workspace_id)
    root = Path(workspace.root_path)
    if not root.is_dir():
        raise ValueError("workspace root_path must be an existing directory")
    return root
