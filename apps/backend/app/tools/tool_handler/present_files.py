"""``present_files``：Agent 把成果文件声明为交付物，回复下方出现文件卡片。

「哪个文件是成果」是只有 Agent 知道的事实：文件操作记录回答「改了哪些」，这里回答「该打开哪个」，
前端不从正文链接或文件操作里猜。工具只校验路径并投影展示数据，不读文件内容、不复制文件、不登记
任何状态；卡片随工具结果行冷重建。
"""

from typing import Any, ClassVar

from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_PRESENT_FILES,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_REPLY_PRESENTATION
from app.tools.display.deliverables_display import build_deliverables_display_data
from app.tools.tool_handler.security.path_resolver import PathResolver
from app.tools.tool_models import PresentFilesArgs
from app.utils.path.file_identity import RootDirectory


class PresentFilesTool(HandlerBase):
    """逐个校验，一个不过不整批失败：通过的照常呈现，没通过的在给模型的结果里点名；全部不过才报错。

    路径规则与 ``read_file`` 一致（``PathResolver.resolve_for_read``：可指向工作区外，拦设备路径与
    禁读的受保护路径），另外要求是已存在的普通文件。
    """

    name = TOOL_PRESENT_FILES
    # 改写自 Claude Code 桌面端 ``SendUserFile`` 的工具描述：去掉与手机推送、展示方式有关的参数。
    description = (
        "Show files to the user as cards under your reply. Use this for any file the user would "
        "want to open — a report, a generated diagram, a document, a built page — when you want "
        "it surfaced, not just mentioned.\n\n"
        "Do not present routine working files: scratch files, logs, intermediate output, or "
        "every file you edited. Edited files already appear in the change record.\n\n"
        "Present a file once it is complete. Present it again only if it has meaningfully "
        "changed. Files must already exist.\n\n"
        "After presenting a file, do not restate its contents or table of contents in your "
        "reply. Give the key takeaways and say what you could not verify."
    )
    args_model = PresentFilesArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_REPLY_PRESENTATION

    def execute(
        self, files: list[dict[str, Any]], *, execution_context: ToolExecutionContext
    ) -> ToolObservation:
        resolver = PathResolver(execution_context.workspace_root)
        workspace = RootDirectory.at(execution_context.workspace_root)
        presented: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        for file in files:
            path = file["path"]
            resolved, error = resolver.resolve_for_read(path)
            if resolved is None:
                failures.append(f"- {path}: {error}")
            elif not resolved.is_file():
                failures.append(f"- {path}: no such file")
            else:
                relative = workspace.relative(resolved)
                # 同一文件写两遍只留后一条说明，卡片不出现重复行。
                presented[str(resolved)] = {
                    "path": relative.as_posix() if relative is not None else str(resolved),
                    "absolute_path": str(resolved),
                    "caption": (file.get("caption") or "").strip() or None,
                }

        if not presented:
            return tool_error(
                self.name,
                "no file could be presented:\n" + "\n".join(failures),
                tool_call_id=execution_context.tool_call_id,
                status_hint="文件不存在",
            )
        content = f"Presented {len(presented)} file(s) to the user."
        if failures:
            content += f"\nNot presented ({len(failures)}):\n" + "\n".join(failures)
        return tool_success(
            self.name,
            content,
            display_data=build_deliverables_display_data(list(presented.values())),
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            # 卡片本身就是这次调用的呈现；调用行只在失败时出现。
            conversation_presentation="quiet",
            name=self.name,
            group=self.group,
            needs_user_surface=True,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("filesystem",),
            execution_mode="thread",
        )


def build_present_files_definition() -> ToolDefinition:
    return PresentFilesTool().to_definition()
