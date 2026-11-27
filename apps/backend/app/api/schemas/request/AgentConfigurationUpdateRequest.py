"""子 Agent 配置更新请求体。"""

from app.api.schemas.request.AgentConfigurationRequest import AgentConfigurationRequest


class AgentConfigurationUpdateRequest(AgentConfigurationRequest):
    """更新是整份替换：在新建请求的字段之外必须带上编辑开始时读到的 ``revision``，对不上就 409。"""

    base_revision: str | None
