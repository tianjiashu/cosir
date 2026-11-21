"""``present_deliverables``：Agent 把成果（文件、目录或网址）声明为交付物，回合里出现一张交付卡。

「哪个是成果」是只有 Agent 知道的事实：文件操作记录回答「改了哪些」，这里回答「该打开哪个」，前端不从正文
链接或文件操作里猜。工具在交付那一刻观察每一项（规范路径、是否在工作区内、文件类型与大小），结果作为
交付记录随结果行持久化（``ToolObservation.facts`` → metadata），不复制文件、不登记任何进程内状态。
工作区内文件的现状由前端现读。

两份事实分两处放：交付记录后端也要读（侧栏产出清单），进 metadata；这批交付的说明（``note``）只给卡片看，
进展示数据 ``{kind: "deliverables", note}``——与 ``offer_next_step`` 的按钮同一种做法。一次调用一张卡，
卡片放在调用的位置，所以模型先交付、再写答案时它就在最终回复上方（排位规则在前端 ``turn-layout``）。
"""

from typing import Any
from urllib.request import url2pathname

from pydantic import ValidationError

from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_PRESENT_DELIVERABLES,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_REPLY_PRESENTATION
from app.models.deliverable import Deliverable, DeliveredDirectory, DeliveredFile, DeliveredUrl
from app.models.tool_result_facts import ToolResultFacts
from app.service.workspace_files.content.file_content import inspect_file
from app.tools.tool_handler.security.path_resolver import PathResolver
from app.tools.tool_models import PresentDeliverablesArgs
from app.utils.path.file_identity import RootDirectory, canonical_path


class PresentDeliverablesTool(HandlerBase):
    """逐项校验与观察，一项不过不整批失败：通过的照常呈现，没通过的在给模型的结果里点名；全部不过才报错。

    路径按 ``resolve_read_scope``（与 ``read_file`` 同一套：可指向工作区外，拦设备路径、受保护路径与
    ``/proc`` 类伪文件树；目录也是合法目标）。观察时的失败（无权访问、磁盘不提供文件号、句柄报出越界的
    名字等）只算这一项失败。同一指向（``Deliverable.key``）写多次只留最后一次，位置也取最后一次。
    """

    name = TOOL_PRESENT_DELIVERABLES
    description = (
        "Show deliverables to the user as cards above your reply: files, directories, or web "
        "addresses. Use this for anything the user would want to open — a report, a generated "
        "diagram, a document, a built site, a page you are serving locally — when you want it "
        "surfaced, not just mentioned.\n\n"
        "Do not present routine working files: scratch files, logs, intermediate output, or every "
        "file you edited. Edited files already appear in the change record. Do not present pages "
        "you only read for research.\n\n"
        "Present something once it is complete. Present it again only if it has meaningfully "
        "changed. Files and directories must already exist.\n\n"
        "Present before you write your answer, in a response with no other text; the cards "
        "appear right above your answer. Use `note` to say what the set is; after presenting, "
        "do not restate the contents or the note in your reply. Give the key takeaways and say "
        "what you could not verify."
    )
    args_model = PresentDeliverablesArgs
    group = TOOL_GROUP_REPLY_PRESENTATION

    def execute(
        self,
        items: list[dict[str, Any]],
        note: str | None = None,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        resolver = PathResolver(execution_context.workspace_root)
        workspace = RootDirectory.at(execution_context.workspace_root)
        presented: dict[str, Deliverable] = {}
        failures: list[str] = []
        for item in items:
            target = item["target"]
            caption = (item.get("caption") or "").strip() or None
            observed, error = self._observe(target, caption, resolver, workspace)
            if observed is None:
                failures.append(f"- {target}: {error}")
                continue
            presented.pop(observed.key, None)
            presented[observed.key] = observed

        if not presented:
            return tool_error(
                self.name,
                "nothing could be presented:\n" + "\n".join(failures),
                tool_call_id=execution_context.tool_call_id,
                status_hint="无法呈现",
            )
        content = f"Presented {len(presented)} item(s) to the user."
        if failures:
            content += f"\nNot presented ({len(failures)}):\n" + "\n".join(failures)
        # 说明只服务卡片：空白当作没有；整批失败时没有卡片，说明也就不写。
        return tool_success(
            self.name,
            content,
            display_data={"kind": "deliverables", "note": (note or "").strip() or None},
            facts=ToolResultFacts(deliverables=tuple(presented.values())),
        )

    @staticmethod
    def _observe(
        target: str, caption: str | None, resolver: PathResolver, workspace: RootDirectory
    ) -> tuple[Deliverable | None, str]:
        """观察一项；返回 ``(记录, "")`` 或 ``(None, 面向模型的原因)``。

        带 ``://`` 的是地址：``file://`` 换算成本机路径、按文件或目录交付（同一个文件只有一种记录，去重、现读与
        出口才都成立）；其余交给 ``DeliveredUrl`` 判定（协议、主机、端口等规则只在那里）。
        """

        scheme, separator, rest = target.partition("://")
        if separator and scheme.lower() != "file":
            try:
                return DeliveredUrl(url=target, caption=caption), ""
            except ValidationError as exc:
                return None, str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
        if separator:
            host, slash, local = rest.partition("/")
            if host not in {"", "localhost"}:
                return None, "file:// addresses must point to this machine"
            target = url2pathname(slash + local)
        resolved, error = resolver.resolve_read_scope(target)
        if resolved is None:
            return None, error
        try:
            absolute = canonical_path(resolved)
            relative = workspace.relative(absolute)
            workspace_path = relative.as_posix() if relative is not None else None
            if resolved.is_dir():
                return DeliveredDirectory(
                    workspace_path=workspace_path, absolute_path=str(absolute), caption=caption
                ), ""
            if not resolved.is_file():
                return None, "no such file or directory"
            # inspect_file 按字面算相对路径，根与路径必须同一写法：工作区内由根拼出，工作区外以真实路径
            # 所在目录为根（交付的是指向别处的链接时，取输入路径的父目录会被句柄校验判为越界）。
            root, path = (
                (workspace.path, workspace.path / relative)
                if relative is not None
                else (absolute.parent, absolute)
            )
            info = inspect_file(root, path)
        except (OSError, ValueError) as exc:
            return None, f"cannot be read ({exc})"
        return DeliveredFile(
            workspace_path=workspace_path,
            absolute_path=str(absolute),
            caption=caption,
            kind=info.kind,
            size=info.size,
            width=info.width,
            height=info.height,
        ), ""

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            # 交付卡本身就是这次调用的呈现；调用行只在失败时出现。
            conversation_presentation="quiet",
            name=self.name,
            group=self.group,
            needs_user_surface=True,
            part_of_reply=True,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
        )


def build_present_deliverables_definition() -> ToolDefinition:
    return PresentDeliverablesTool().to_definition()
