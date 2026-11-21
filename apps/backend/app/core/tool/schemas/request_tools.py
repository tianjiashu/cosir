"""一次模型请求的工具集：发给模型的定义与工具执行层的准入名单，同一处推导、同源。

发给模型的工具列表永远只是 Session 工具面里不延迟的基础 schema：延迟工具（内置与外部）加载后，定义只在
``tool_search`` 的结果（一条 tool 消息）里，模型照着它直接按名字调用；工具列表是请求前缀的最前端，不往里加，
加载就不会打断提示词缓存。代价是延迟工具的参数不受服务端 strict 约束，由准入门禁按参数模型 / JSON Schema
在本地校验兜底。

准入名单 = 基础工具 + 本 Session 上下文里已加载的延迟工具（``tool_search`` 结果行的 ``facts``），再减去本 Run
禁用的工具。准入不绑在模型对象上：延迟工具在一次驱动中途加载，下一批调用就要放行，所以每个消费方在用到的那一刻
现取（``RuntimeContext.request_tools``）。消费方：

- ``model_node``、``structured_output_node`` 与标题支线：请求带 ``schemas``（后两者 ``tool_choice="none"``），
  三者工具前缀一致，才能接上提示词缓存；
- ``tools_node``：整批按同一份准入。同一批里先检索、后调用刚加载的工具，后者被拒，下一批才放行——加载结果要先
  进上下文，模型才看得到定义。

准入只拦截、不过滤定义：本 Run 不准入的工具（派生限制，见 ``RuntimeContext.banned_tools``）仍在
``schemas`` 里——改动定义会改变请求前缀、接不上缓存，「为什么不能用」由工具准入通知告诉模型。
「已加载」只从上下文推导、不另存，所以分叉随上下文继承、撤回回合即卸载、恢复与重启照样成立。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from openai.types.shared_params import FunctionDefinition

from app.models.external_tool import ExternalToolDefinition


@dataclass(frozen=True)
class RequestTools:
    """不可变值对象；``schemas`` 只有基础工具，``admitted`` 另含已加载的延迟工具。

    ``external`` 是已准入外部工具的定义快照（执行时交给端口）；
    ``unloaded`` 是 Session 能延迟加载、但还没加载的内置工具（门禁据此提示先加载）；
    ``deferred_schemas`` 与 ``loaded_builtin`` 给 ``tool_search``：前者是目录里的
    内置部分，两者合起来判断一次检索是不是在重复加载。
    """

    schemas: tuple[FunctionDefinition, ...]
    admitted: frozenset[str]
    external: Mapping[str, ExternalToolDefinition] = field(default_factory=dict)
    unloaded: frozenset[str] = frozenset()
    deferred_schemas: tuple[FunctionDefinition, ...] = ()
    loaded_builtin: frozenset[str] = frozenset()

    @classmethod
    def derive(
        cls,
        base_schemas: tuple[FunctionDefinition, ...],
        banned: frozenset[str],
        *,
        deferred_schemas: tuple[FunctionDefinition, ...] = (),
        loaded_builtin: Collection[str] = (),
        loaded_external: Iterable[ExternalToolDefinition] = (),
    ) -> RequestTools:
        """工具列表取基础 schema，准入名单再并上已加载的延迟工具，减去本 Run 禁用的工具名。

        外部工具 ``loaded_external`` 按加载先后给出、可以重名（同一个工具加载过几次）：定义取最后一次——重新加载
        拿到服务的新版本。与基础工具同名的忽略：基础工具的定义固化在 Session 里，不能被覆盖。
        """
        base_names = {schema["name"] for schema in base_schemas}
        builtin = {s["name"] for s in deferred_schemas if s["name"] in loaded_builtin}
        external: dict[str, ExternalToolDefinition] = {}
        for definition in loaded_external:
            if definition.name not in base_names:
                external[definition.name] = definition
        return cls(
            schemas=base_schemas,
            admitted=frozenset(base_names | builtin | external.keys()) - banned,
            external={name: d for name, d in external.items() if name not in banned},
            unloaded=frozenset(s["name"] for s in deferred_schemas if s["name"] not in builtin) - banned,
            deferred_schemas=deferred_schemas,
            loaded_builtin=frozenset(builtin),
        )
