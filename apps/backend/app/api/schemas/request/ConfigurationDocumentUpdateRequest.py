"""整份文本配置文档（全局 / 工作区 AGENTS.md、主 Agent 提示词、.fileignore）的保存请求体。"""

from pydantic import BaseModel, ConfigDict


class ConfigurationDocumentUpdateRequest(BaseModel):
    """新正文与它所基于的版本。

    ``base_revision`` 是前端开始编辑时读到的 ``revision``（文件不存在时为 ``null``），必填：整份替换
    没有按字段合并的余地，基准对不上就 409，交给用户选覆盖还是载入。
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    base_revision: str | None
