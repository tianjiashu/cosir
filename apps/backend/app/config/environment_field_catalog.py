"""环境配置中心的白名单字段目录。

``ENVIRONMENT_GROUPS`` 决定配置中心的展示分组与顺序，``ENVIRONMENT_FIELDS`` 是字段名校验与
读写的唯一白名单来源：未在此登记的环境变量既不会被读取，也不允许被写入。

本模块只承载静态元数据，不读取磁盘、不校验字段值、不感知当前进程配置与运行时状态；值的类型校验
和 ``.env`` 读写由 ``EnvironmentConfigurationService`` 承担。新增字段时其 ``group_id`` 必须已
出现在 ``ENVIRONMENT_GROUPS`` 中，否则 service 在 ``read_grouped`` 时抛出配置契约错误。
"""

from app.config.constant import Constant
from app.models.environment.environment_field import EnvironmentField
from app.models.environment.environment_group import EnvironmentGroup

ENVIRONMENT_GROUPS: tuple[EnvironmentGroup, ...] = (
    EnvironmentGroup("general", "基础配置", "控制 Agent 的通用运行行为。"),
    EnvironmentGroup("web", "网页工具", "配置网页搜索与正文提取使用的 Firecrawl 凭证和服务地址。"),
    EnvironmentGroup("langfuse", "Langfuse 可观测性", "配置 Agent 与工具调用链路追踪。"),
)

ENVIRONMENT_FIELDS: dict[str, EnvironmentField] = {
    "DEFAULT_LANGUAGE": EnvironmentField(
        "DEFAULT_LANGUAGE",
        "string",
        "select",
        "general",
        "默认回复语言",
        "Agent 默认使用的回复语言。",
        False,
        "zh",
        (("", "使用系统默认值（中文）"), ("zh", "中文"), ("en", "English")),
        "请选择默认语言",
    ),
    "FIRECRAWL_API_KEY": EnvironmentField(
        "FIRECRAWL_API_KEY",
        "string",
        "password",
        "web",
        "API Key",
        "Firecrawl 访问密钥；使用官方服务时必填，自建无鉴权网关可留空。",
        True,
        None,
        placeholder="请输入 Firecrawl API Key",
    ),
    "FIRECRAWL_API_URL": EnvironmentField(
        "FIRECRAWL_API_URL",
        "string",
        "input",
        "web",
        "服务地址",
        f"Firecrawl API 根地址；留空使用官方地址 {Constant.Web.FIRECRAWL_DEFAULT_BASE_URL}。",
        False,
        "",
        placeholder=Constant.Web.FIRECRAWL_DEFAULT_BASE_URL,
    ),
    "LANGFUSE_ENABLED": EnvironmentField(
        "LANGFUSE_ENABLED",
        "boolean",
        "checkbox",
        "langfuse",
        "启用 Langfuse",
        "记录 Agent、模型和工具调用的可观测性数据。",
        False,
        False,
    ),
    "LANGFUSE_PUBLIC_KEY": EnvironmentField(
        "LANGFUSE_PUBLIC_KEY",
        "string",
        "password",
        "langfuse",
        "Public Key",
        "Langfuse 项目的公开访问密钥。",
        True,
        None,
        placeholder="请输入 Public Key",
    ),
    "LANGFUSE_SECRET_KEY": EnvironmentField(
        "LANGFUSE_SECRET_KEY",
        "string",
        "password",
        "langfuse",
        "Secret Key",
        "Langfuse 项目的私密访问密钥。",
        True,
        None,
        placeholder="请输入 Secret Key",
    ),
    "LANGFUSE_BASE_URL": EnvironmentField(
        "LANGFUSE_BASE_URL",
        "string",
        "input",
        "langfuse",
        "服务地址",
        "Langfuse 服务的访问地址。",
        False,
        "http://124.220.55.187",
        placeholder="https://langfuse.example.com",
    ),
}
