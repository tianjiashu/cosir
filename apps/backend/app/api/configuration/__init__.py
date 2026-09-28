"""系统配置中心 HTTP API 包。

按子域拆成四个路由模块，共用一份错误映射：

- :mod:`app.api.configuration.agents`：``/configuration/agents``（子 Agent 文档增删改查，含
  「工具组 ↔ 工具名」双向投影）；
- :mod:`app.api.configuration.global_instructions`：``/configuration/global-instructions``；
- :mod:`app.api.configuration.main_agent_prompt`：``/configuration/main-agent-prompt``；
- :mod:`app.api.configuration.environment`：``/configuration/environment``；
- :mod:`app.api.configuration.errors`：四个子域共用的「领域异常 → HTTP 错误」映射。

路由仍以模块级 ``@app.*`` 装饰器注册到 ``app.app`` 的单例 FastAPI：由 ``app.app`` 在定义
``app`` 之后显式导入各子模块，导入顺序即注册顺序。本包不在 ``__init__`` 中隐式导入子模块，
避免「导入包即产生路由注册副作用」这种不可见行为。

请求与响应 schema 位于 ``app.api.schemas.request`` / ``app.api.schemas.response``；文件事实由
``app.service.configuration`` 下的 service 管理。Agent runtime、模型连接配置与 Assistant
Transport 不经过本包。
"""
