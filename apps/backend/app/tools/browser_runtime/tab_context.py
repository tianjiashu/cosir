"""浏览器标签上下文：宿主管理标签时要用、但只有后端知道的事实。

宿主不存配置、不知道 Session 属于哪个工作区、也不知道所有权，所以标签上限（事实源是配置中心 ``Settings``，
每次现读，保存配置后下一次请求即生效）、下载目录（Session 所在工作区的 ``.cosir/Download``）与显示会话
（标签显示在哪个会话页上，规则只在 ``SessionService.display_session_id``：子 Agent 的标签归最近的面向用户的
会话，分叉是它自己）由后端在每次请求时带过去。两条路径共用这里的解析：Agent 路径随宿主工具链路参数注入
（``call_browser``），用户路径（用户在面板里新开标签）由前端经 ``GET /sessions/{id}/browser-context`` 取来交给宿主。

只解析路径，不创建下载目录：目录由宿主在真正落盘前按需创建。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.core.tool.schemas import ToolExecutionContext
from app.service import depends as service_depends
from app.utils.path.workspace_cosir import workspace_download_dir


@dataclass(frozen=True, slots=True)
class BrowserTabLimits:
    per_session: int
    total: int


@dataclass(frozen=True, slots=True)
class BrowserTabContext:
    """``dataclasses.asdict`` 后即宿主工具链路参数里的 ``limits``、``download_dir`` 与 ``display_session_id``。"""

    limits: BrowserTabLimits
    # 本机绝对路径：宿主不知道工作区，也不该按自己的工作目录解析相对路径。
    download_dir: str
    # 标签显示在哪个会话页上：页头入口据此把子 Agent 在用浏览器也算作这个会话在用。只是显示归属，
    # Agent 能操作哪些标签、面板显示哪些标签仍按所属 Session。
    display_session_id: int

    @classmethod
    def for_tool_call(cls, execution_context: ToolExecutionContext) -> BrowserTabContext:
        """Agent 路径：工作区根随执行上下文带来，显示会话按所有权现查。"""

        return cls._resolve(execution_context.session_id, execution_context.workspace_root)

    @classmethod
    def for_session(cls, session_id: int) -> BrowserTabContext:
        """用户路径：按 Session 查出工作区。

        异常:
            KeyError: Session 或其工作区不存在。
        """

        session = service_depends.get_session_service().get_session(session_id)
        workspace = service_depends.get_workspace_service().get_workspace(session.workspace_id)
        return cls._resolve(session_id, workspace.root_path)

    @classmethod
    def _resolve(cls, session_id: int, workspace_root: str | Path) -> BrowserTabContext:
        return cls(
            limits=BrowserTabLimits(
                per_session=Settings.BROWSER_MAX_TABS_PER_SESSION,
                total=Settings.BROWSER_MAX_TABS,
            ),
            download_dir=str(workspace_download_dir(Path(workspace_root).resolve())),
            display_session_id=service_depends.get_session_service().display_session_id(session_id),
        )
