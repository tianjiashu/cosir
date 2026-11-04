"""浏览器标签上下文：宿主管理标签时要用、但只有后端知道的事实。

宿主不存配置、不知道 Task 属于哪个工作区，所以标签上限（事实源是配置中心 ``Settings``，每次现读，
保存配置后下一次请求即生效）与下载目录（Task 所在工作区的 ``.cosir/Download``）由后端在每次请求时
带过去。两条路径共用这里的解析：Agent 路径随宿主工具链路参数注入（``call_browser``），
用户路径（用户在面板里新开标签）由前端经 ``GET /tasks/{id}/browser-context`` 取来交给宿主。

只解析路径，不创建下载目录：目录由宿主在真正落盘前按需创建。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.service import depends as service_depends
from app.utils.path.workspace_cosir import workspace_download_dir


@dataclass(frozen=True, slots=True)
class BrowserTabLimits:
    per_task: int
    total: int


@dataclass(frozen=True, slots=True)
class BrowserTabContext:
    """``dataclasses.asdict`` 后即宿主工具链路参数里的 ``limits`` 与 ``download_dir``。"""

    limits: BrowserTabLimits
    # 本机绝对路径：宿主不知道工作区，也不该按自己的工作目录解析相对路径。
    download_dir: str

    @classmethod
    def from_workspace(cls, workspace_root: str | Path) -> BrowserTabContext:
        """按工作区根目录解析；工具执行上下文已带工作区根，不必再查库。"""

        return cls(
            limits=BrowserTabLimits(
                per_task=Settings.BROWSER_MAX_TABS_PER_TASK,
                total=Settings.BROWSER_MAX_TABS,
            ),
            download_dir=str(workspace_download_dir(Path(workspace_root).resolve())),
        )

    @classmethod
    def from_task(cls, task_id: int) -> BrowserTabContext:
        """按 Task 解析（用户路径）。

        异常:
            KeyError: Task 或其工作区不存在。
        """

        task = service_depends.get_task_service().get_task(task_id)
        workspace = service_depends.get_workspace_service().get_workspace(task.workspace_id)
        return cls.from_workspace(workspace.root_path)
