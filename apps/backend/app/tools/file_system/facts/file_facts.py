"""一次工具调用交出的文件事实：读到的版本与确认的写入。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.message_context.message_metadata.tool_message_metadata import (
    FileFactsPayload,
    ToolResultFacts,
)
from app.tools.file_system.facts.file_mutation import FileMutation
from app.tools.file_system.facts.file_read import FileRead


class FileFacts(BaseModel):
    """文件工具交出、随结果行落库的那部分事实；只有文件系统解读它（``FileKnowledge``）。

    core 只见它的 JSON 形态（``ToolResultFacts.file_facts``），存放、透传、随上下文继承与移出，不解析；
    所以形态的生产与解析都在本类（:meth:`as_result` / :meth:`of_payload`）。没读到、没写成文件时不交
    （``None``）。终端对文件系统不透明，不交文件事实。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reads: tuple[FileRead, ...] = ()
    mutations: tuple[FileMutation, ...] = ()

    def as_result(self) -> ToolResultFacts:
        """包成交给 core 的结果事实。"""
        return ToolResultFacts(file_facts=self.model_dump(mode="json"))

    @classmethod
    def of_payload(cls, payload: FileFactsPayload) -> FileFacts:
        return cls.model_validate(payload)
