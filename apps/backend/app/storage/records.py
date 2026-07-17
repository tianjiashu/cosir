"""存储后端使用的持久化记录值对象。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


def datetime_to_text(value: datetime) -> str:
    """将 datetime 值转换为 ISO-8601 文本。

    参数:
        value: 待序列化的 datetime 值。

    返回:
        ISO-8601 datetime 字符串。

    异常:
        无。

    副作用:
        无。
    """

    return value.isoformat()


@dataclass
class WorkspaceRecord:
    """表示一个本地工作区。

    参数:
        workspace_id: 唯一的工作区标识符。
        name: 用户可读的工作区名称。
        root_path: 工作区的本地文件系统路径。
        created_at: 工作区创建时的时间戳。
        updated_at: 工作区最近更新时的时间戳。

    返回:
        一个工作区状态记录。

    异常:
        无。

    副作用:
        无。
    """

    workspace_id: str
    name: str
    root_path: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, str]:
        """将工作区状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            工作区状态的字典表示。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "root_path": self.root_path,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }


@dataclass
class TaskRecord:
    """表示一个 Agent 任务的持久化状态。

    参数:
        task_id: 唯一的任务标识符。
        workspace_id: 与任务关联的工作区标识符。
        agent_id: 负责执行任务的 Agent 标识符。
        input_text: 原始的纯文本用户任务。
        title: 任务容器标题。
        last_message_preview: 最近用户输入摘要。
        latest_turn_id: 最近一次轮次标识符。
        status: 当前任务状态。
        created_at: 任务创建时的 UTC 时间戳。
        updated_at: 任务最近更新时的 UTC 时间戳。

    返回:
        一个可变的任务状态记录。

    异常:
        无。

    副作用:
        无。
    """

    task_id: str
    workspace_id: str
    agent_id: str
    input_text: str
    title: str
    last_message_preview: str
    latest_turn_id: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Optional[str]]:
        """将任务状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            任务状态的字典表示。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "agent_id": self.agent_id,
            "input_text": self.input_text,
            "title": self.title,
            "last_message_preview": self.last_message_preview,
            "latest_turn_id": self.latest_turn_id,
            "status": self.status,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }


@dataclass
class TurnRecord:
    """表示一次用户与 Agent 的轮次。

    参数:
        turn_id: 唯一的轮次标识符。
        task_id: 与该轮次关联的任务标识符。
        input_text: 该轮次的用户文本。
        status: 当前轮次状态。
        created_at: 轮次创建时的时间戳。
        updated_at: 轮次最近更新时的时间戳。

    返回:
        一个轮次状态记录。

    异常:
        无。

    副作用:
        无。
    """

    turn_id: str
    task_id: str
    input_text: str
    status: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, str]:
        """将轮次状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            轮次状态的字典表示。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "turn_id": self.turn_id,
            "task_id": self.task_id,
            "input_text": self.input_text,
            "status": self.status,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }


@dataclass
class StepRecord:
    """表示一个持久化的运行时步骤。

    参数:
        step_id: 唯一的步骤标识符。
        turn_id: 与该步骤关联的轮次标识符。
        step_type: 运行时步骤类型。
        status: 当前步骤状态。
        input_summary: 用于诊断的简短输入摘要。
        output_summary: 用于诊断的简短输出摘要。
        error: 可选的错误消息。
        created_at: 步骤创建时的时间戳。
        updated_at: 步骤最近更新时的时间戳。

    返回:
        一个步骤状态记录。

    异常:
        无。

    副作用:
        无。
    """

    step_id: str
    turn_id: str
    step_type: str
    status: str
    input_summary: str
    output_summary: str
    error: Optional[str]
    created_at: datetime
    updated_at: datetime


@dataclass
class CheckpointRecord:
    """表示一个持久化的运行时状态检查点。

    参数:
        checkpoint_id: 唯一的检查点标识符。
        task_id: 与该检查点关联的任务标识符。
        stage: 产出该检查点的运行时阶段。
        summary: 简短的、人类可读的检查点摘要。
        snapshot: 可序列化为 JSON 的运行时状态快照。
        created_at: 检查点创建时的时间戳。

    返回:
        一个检查点状态记录。

    异常:
        无。

    副作用:
        无。
    """

    checkpoint_id: str
    task_id: str
    stage: str
    summary: str
    snapshot: Dict[str, Any]
    created_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        """将检查点状态转换为可序列化为 JSON 的字典。

        参数:
            无。

        返回:
            检查点记录的字典表示。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "checkpoint_id": self.checkpoint_id,
            "task_id": self.task_id,
            "stage": self.stage,
            "summary": self.summary,
            "snapshot": self.snapshot,
            "created_at": datetime_to_text(self.created_at),
        }
