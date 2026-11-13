"""用户输入解析边界：模型能力校验、胶囊指针校验与 Skill 路径补全，产出待入队的消息体。

HTTP 发送使用；Agent 之间的输入走 ``agent_message``。只读 Session、模型配置、工作区与已发布图片的
元数据，不写库、不入队、不创建 Run。投递方在入队之前调用（校验 IO 不占用 Session 闸门），输入不合法
在这里同步拒绝，不让坏数据进外部队列。

正文里的 ``<file>`` / ``<ref>`` / ``<skill>`` 是用户给的指针：这里校验指针可用、把 Skill 的
``SKILL.md`` 绝对路径补回标签，正文本身不替换成文件或 Skill 内容——模型自己按指针去读。
"""

import os
import stat
from collections import Counter
from pathlib import Path

from app.config.logging.logger import log
from app.core.user_input.capsules import (
    Capsule,
    CapsuleSegment,
    CapsuleSegmentLike,
    FileCapsule,
    RefCapsule,
    SkillCapsule,
    parse_capsules,
    serialize_segments,
)
from app.core.workflow.message_queue.session_message import RunLaunch, UserInputBody
from app.models import RunCommand
from app.models.conversation_input import UserInputPresentation
from app.models.run_options import RunOptions, ReasoningEffort
from app.models.errors.input_errors import UserInputRejectedError
from app.service.attachment.attachment_service import AttachmentService
from app.service.configuration.skill_catalog import SkillCatalog
from app.service.configuration.skill_configuration import SkillConfigurationError
from app.service.depends import get_model_config_service, get_session_crud, get_workspace_service


class UserInputService:
    """进程级单例（``service.depends.get_user_input_service``），无进程内状态。"""

    def __init__(self) -> None:
        self._session = get_session_crud()

    def prepare(
        self,
        session_id: int,
        command: RunCommand,
        model_config_id: str,
        reasoning_effort: ReasoningEffort | None,
        agent_id: str,
    ) -> UserInputBody:
        """校验模型能力与胶囊指针，组装成携带发起配置的用户输入消息体。

        投递方式（``delivery``）是信封属性，由入队方按 Session 当时的阶段决定后自己包成 ``SessionMessage``。
        空输入由请求模型在 HTTP 边界拒绝（文本 part 不能为空白，至少一个 part），这里不再重复。

        异常:
            UserInputRejectedError: 模型配置不存在、模型不支持所选能力、胶囊指向的文件 / 引用 /
                Skill 不可用。
            ImageNormalizationError: 本次图片数量或总大小超过统一输入限制。
            KeyError: Session 不存在（接入层映射为 404）。
        """
        session = self._session.get(session_id)
        config = get_model_config_service().check_launch_config(model_config_id, reasoning_effort)
        if command.image_asset_ids and not config.model.supports_image:
            raise UserInputRejectedError(
                "IMAGE_NOT_SUPPORTED", "当前模型不支持图片输入，请更换模型或移除图片"
            )
        segments = self._validated_segments(command.text, session.workspace_id)
        text = serialize_segments(segments)
        image_paths = self._resolve_image_assets(session.workspace_id, command.image_asset_ids)
        counts = Counter(
            segment.capsule.kind for segment in segments if isinstance(segment, CapsuleSegment)
        )
        log.info(
            "user_input_capsules_validated",
            extra={
                "msg": "用户输入胶囊已校验",
                "data": {
                    "session_id": session_id,
                    "file": counts["file"],
                    "ref": counts["ref"],
                    "skill": counts["skill"],
                },
            },
        )
        return UserInputBody(
            text=text,
            presentation=UserInputPresentation(text=text, image_paths=image_paths),
            launch=RunLaunch(
                agent_id=agent_id,
                model_config_id=model_config_id,
                options=RunOptions(
                    disabled_tool_groups=command.disabled_tool_groups,
                    reasoning_effort=reasoning_effort,
                ),
            ),
        )

    @classmethod
    def _validated_segments(cls, text: str, workspace_id: int) -> list[CapsuleSegmentLike]:
        """校验正文里的胶囊并补全 Skill 路径。

        语法不合法的标签已被解析器当普通文本保留，不在这里拒绝。
        """
        workspace_root = Path(get_workspace_service().get_workspace(workspace_id).root_path)
        return [
            CapsuleSegment(capsule=cls._validate_capsule(segment.capsule, workspace_root))
            if isinstance(segment, CapsuleSegment)
            else segment
            for segment in parse_capsules(text)
        ]

    @classmethod
    def _validate_capsule(cls, capsule: Capsule, workspace_root: Path) -> Capsule:
        if isinstance(capsule, FileCapsule):
            cls._check_local_file(capsule.path)
            return capsule
        if isinstance(capsule, RefCapsule):
            cls._check_workspace_ref(capsule.path, workspace_root)
            return capsule
        return SkillCapsule(name=capsule.name, path=cls._skill_file(capsule.name, workspace_root))

    @staticmethod
    def _check_local_file(path: str) -> None:
        """校验本机文件附件：一次 stat 区分「不存在」与「不可访问」，并按尾斜杠核对目录形态。

        不存在是用户可修正的失效（重新选择附件）；其它 ``OSError``（权限、网络盘不可达）必须报
        「无法访问」——把它说成不存在会误导用户去重新选择一个仍然有效的文件。
        """
        # `<file>` 的契约是本机绝对路径（前端由宿主原生选择器给出）。相对路径会在后端进程 cwd
        # 下解析，语义取决于启动方式，直接拒绝而不是猜。
        if not Path(path).is_absolute():
            raise UserInputRejectedError(
                "CAPSULE_FILE_UNAVAILABLE",
                "附件已失效，请重新选择附件",
                detail=f"file capsule path must be absolute: {path}",
            )
        try:
            mode = os.stat(path).st_mode
        except (FileNotFoundError, NotADirectoryError) as exc:
            # 尾斜杠写在文件上时 ``os.stat`` 报 ``NotADirectoryError``：用户声明的目录不存在，
            # 与路径不存在同归「附件已失效」。
            raise UserInputRejectedError(
                "CAPSULE_FILE_UNAVAILABLE",
                "附件已失效，请重新选择附件",
                detail=f"file capsule path does not exist: {path}",
            ) from exc
        except OSError as exc:
            raise UserInputRejectedError(
                "CAPSULE_FILE_UNREADABLE",
                "附件无法访问，请重新选择附件",
                detail=f"file capsule path cannot be accessed: {path}: {exc}",
            ) from exc
        is_directory = stat.S_ISDIR(mode)
        # 目录必须以 ``/`` 结尾：前端据此区分「文件」与「目录」两种胶囊，模型也据此决定读取方式。
        if is_directory and not path.endswith("/"):
            raise UserInputRejectedError(
                "CAPSULE_FILE_UNAVAILABLE",
                "附件已失效，请重新选择附件",
                detail=f"directory capsule path must end with '/': {path}",
            )
        if path.endswith("/") and not is_directory:
            raise UserInputRejectedError(
                "CAPSULE_FILE_UNAVAILABLE",
                "附件已失效，请重新选择附件",
                detail=f"file capsule path is not a directory: {path}",
            )

    @staticmethod
    def _check_workspace_ref(path: str, workspace_root: Path) -> None:
        """校验工作区引用：路径必须相对工作区且不越界，解析后必须存在。

        空 ``path`` 表示工作区根。只做词法越界判断，不解析符号链接：引用是用户自己看到的相对
        路径，工作区文件工具才是真正读取它的地方。
        """
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise UserInputRejectedError(
                "CAPSULE_REF_UNAVAILABLE",
                "引用已失效，请重新选择引用",
                detail=f"ref capsule path is not workspace-relative: {path}",
            )
        try:
            (workspace_root / relative).stat()
        except OSError as exc:
            raise UserInputRejectedError(
                "CAPSULE_REF_UNAVAILABLE",
                "引用已失效，请重新选择引用",
                detail=f"ref capsule path does not exist: {path}: {exc}",
            ) from exc

    @staticmethod
    def _skill_file(name: str, workspace_root: Path) -> str:
        """按 Session 所在工作区解析用户选中的 Skill，返回 ``SKILL.md`` 的绝对路径。

        名称按工作区解析（工作区同名覆盖系统级），不存在或已停用时同步拒绝，前端收回乐观消息
        并恢复草稿。模型从路径或 ``skill`` 工具读正文，不在这里把正文固化进上下文。
        """
        catalog = SkillCatalog.visible_from(workspace_root)
        try:
            info = catalog.find(name)
        except (KeyError, SkillConfigurationError) as exc:
            raise UserInputRejectedError(
                "SKILL_UNAVAILABLE",
                f"Skill「{name}」不可用，请重新选择",
                detail=f"skill {name!r} cannot be resolved: {exc}",
            ) from exc
        if not info.enabled:
            raise UserInputRejectedError(
                "SKILL_UNAVAILABLE",
                f"Skill「{name}」不可用，请重新选择",
                detail=f"skill {name!r} is disabled",
            )
        return str(info.skill_file)

    @staticmethod
    def _resolve_image_assets(workspace_id: int, image_asset_ids: list[str]) -> list[str]:
        """校验本次图片聚合限制，并返回已上传图片的 workspace-relative 路径。"""

        if not image_asset_ids:
            return []
        return AttachmentService().resolve_image_assets_for_run(workspace_id, image_asset_ids)
