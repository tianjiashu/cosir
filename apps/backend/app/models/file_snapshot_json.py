"""文件快照持久化 JSON 的结构类型。"""

from typing import Literal, NotRequired

from typing_extensions import TypedDict


class FileSnapshotStateJson(TypedDict):
    """单个工作区路径在一次操作前或操作后的可恢复状态。"""

    exists: bool
    entry_type: NotRequired[Literal["file", "directory", "symlink"]]
    sha256: NotRequired[str]
    restore_ref: NotRequired[str]
    target: NotRequired[str]
    target_is_directory: NotRequired[bool]


class FileSnapshotPathStateJson(TypedDict):
    """变更集中一个路径的身份及前后状态。"""

    file_identity: str
    path: str
    before: FileSnapshotStateJson
    after: FileSnapshotStateJson


class FileSnapshotOperationJson(TypedDict):
    """快照对应的文件工具操作信息。"""

    type: Literal["ADD", "DELETE", "UPDATE", "MOVE", "PREPARED"]
    operation_id: str
    tool: NotRequired[str]


class FileSnapshotDisplayPatchJson(TypedDict):
    """供变更集界面展示的差异摘要。

    ``format`` 语义：``unified`` 为文本行差异、``rename`` 为纯移动元数据、``binary`` 为
    不可按文本解码的目标、``deleted`` 为删除（操作内容不进入展示，``text`` 恒为空串）。
    """

    format: Literal["unified", "rename", "binary", "deleted"]
    text: str
    truncated: bool


class FileSnapshotSideEffectStateJson(TypedDict):
    """文件操作创建的隐式父目录状态。"""

    path: str
    before: FileSnapshotStateJson
    after: FileSnapshotStateJson
    directory_id: str


class FileSnapshotOpJson(TypedDict):
    """``file_snapshots.op_json`` 中单条文件变更的状态封装。"""

    operation: FileSnapshotOperationJson
    path_states: list[FileSnapshotPathStateJson]
    display_patch: NotRequired[FileSnapshotDisplayPatchJson]
    side_effect_states: NotRequired[list[FileSnapshotSideEffectStateJson]]


class FileSnapshotStagingJson(TypedDict):
    """预写操作暂存的 before-image 对象引用。"""

    id: str
    sha256: str


class FileSnapshotMutationJson(TypedDict):
    """``file_snapshots.mutation_json`` 中的预写操作恢复清单。"""

    kind: Literal["agent_mutation"]
    workspace_id: int
    snapshot_paths: list[str]
    implicit_directory_paths: list[str]
    move_pairs: list[list[str]]
    staging: list[FileSnapshotStagingJson]


__all__ = ["FileSnapshotMutationJson", "FileSnapshotOpJson"]
