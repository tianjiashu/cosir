"""模型目录发现服务（litellm model list 统一入口，D3）。

单一职责：读取 litellm 内置模型目录，按厂商类型前缀过滤出候选模型列表，
供前端勾选导入。本模块是 litellm 目录读取的**唯一入口**之一（另一处为
``core/llm/factory`` 的 ChatLiteLLM 构建，见设计文档 §11 依赖边界）。

职责边界：
- 负责：litellm 目录惰性读取、type → 前缀过滤（派生自注册表）、候选值对象
  组装、``already_imported`` 标注、成功 / 失败可排查日志。
- 不负责：候选入库（``model_entry_service.import_models``）、实际连通性测试
  （目录 = litellm 支持 ≠ 厂商实际提供，设计文档 §14）。

失败语义（§7.1）：litellm 目录读取异常 → error 日志
``provider_discover_failed`` + 抛 ``ProviderDiscoverError``（API 层转 502
级响应），不静默返回空列表。

2026-08-18 重构：``PROVIDER_TYPE_PREFIXES`` 硬编码 dict 删除，前缀改由
``ProviderCapability.litellm_prefix`` 决定（注册表 §三 单一事实源）。
新增厂商无需改本文件——只改注册表一行即可。
"""

from dataclasses import dataclass
from time import perf_counter

from app.config.logging.logger import log
from app.models import ProviderRecord
from app.models.provider_capability import get_capability
from app.service import depends as service_depends
from app.storage.crud.model_entry_crud import ModelEntryCrud

#: 目录条目缺失窗口信息时的兜底窗口（litellm 绝大多数条目带
#: max_input_tokens；缺失时用保守值预填，用户可在导入前修改）。
_FALLBACK_CONTEXT_WINDOW = 8192


@dataclass(frozen=True)
class ModelCandidate:
    """一个待勾选导入的候选模型（discover 产物）。

    属性:
        model_name: litellm 路由名（如 ``deepseek/deepseek-v4-flash``）。
        display_name: 去前缀后的下拉展示名。
        max_context_window: litellm 已知的上下文窗口（token），预填可改。
        supports_thinking: litellm 目录标注的推理模型标识。
        already_imported: 该厂商下是否已存在同名条目（前端置灰 / 预勾选依据）。
    """

    model_name: str
    display_name: str
    max_context_window: int
    supports_thinking: bool
    already_imported: bool


class ProviderDiscoverError(RuntimeError):
    """litellm 模型目录读取失败（外部依赖异常，API 层转 502 级响应）。"""


class ProviderDiscoverService:
    """litellm 模型目录发现服务。"""

    def __init__(self) -> None:
        """初始化发现服务。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 ModelEntryCrud 单例（用于
            ``already_imported`` 标注）并保存引用。
        """

        self._model_entry_crud: ModelEntryCrud = service_depends.get_model_entry_crud()

    def discover_models(self, provider: ProviderRecord) -> list[ModelCandidate]:
        """读取 litellm 目录并按厂商类型过滤出候选模型列表。

        前缀由 ``ProviderCapability.litellm_prefix`` 决定（注册表 §三 单一
        事实源）；``None`` 前缀（如 ``custom``）不过滤，返回全目录由用户
        搜索勾选。候选按模型名升序、已导入项排后，``already_imported`` 以
        该厂商下既有 ``model_name`` 集合标注。

        参数:
            provider: 目标厂商记录（提供 ``provider_type`` 与 ``provider_id``）。

        返回:
            候选模型列表（可能为空列表：目录中无该前缀的条目属正常结果，
            与「目录读取失败」的 502 语义区分）。

        异常:
            ProviderDiscoverError: litellm 目录读取失败（内部异常统一归一）。

        副作用:
            惰性导入 litellm 并读取内置目录（首次调用有秒级开销）；写
            info 日志 ``provider_discover_succeeded`` 或 error 日志
            ``provider_discover_failed``。
        """

        started = perf_counter()
        capability = get_capability(provider.provider_type)
        prefix = capability.litellm_prefix
        try:
            cost_map = _load_litellm_cost_map()
        except Exception as exc:
            elapsed_ms = int((perf_counter() - started) * 1000)
            log.error(
                "provider_discover_failed",
                extra={
                    "msg": (f"litellm 模型目录读取失败：{type(exc).__name__}: {exc}"),
                    "data": {
                        "provider_id": provider.provider_id,
                        "type": provider.provider_type,
                        "error_type": type(exc).__name__,
                        "elapsed_ms": elapsed_ms,
                        "retryable": True,
                    },
                },
            )
            raise ProviderDiscoverError(
                f"litellm model catalog unavailable: {type(exc).__name__}: {exc}"
            ) from exc

        existing_names = {
            record.model_name
            for record in self._model_entry_crud.list_by_provider(provider.provider_id)
        }
        candidates: list[ModelCandidate] = []
        for model_name, entry in cost_map.items():
            if not isinstance(entry, dict):
                continue
            if prefix is not None and not model_name.startswith(prefix):
                continue
            display_name = model_name[len(prefix) :] if prefix else model_name
            max_window = entry.get("max_input_tokens")
            candidates.append(
                ModelCandidate(
                    model_name=model_name,
                    display_name=display_name,
                    max_context_window=(
                        int(max_window)
                        if isinstance(max_window, int | float) and max_window > 0
                        else _FALLBACK_CONTEXT_WINDOW
                    ),
                    supports_thinking=bool(entry.get("supports_reasoning", False)),
                    already_imported=model_name in existing_names,
                )
            )
        # 未导入在前（可勾选主体）、已导入在后；组内按模型名升序稳定排序。
        candidates.sort(key=lambda item: (item.already_imported, item.model_name))

        elapsed_ms = int((perf_counter() - started) * 1000)
        log.info(
            "provider_discover_succeeded",
            extra={
                "msg": (
                    f"litellm 目录发现完成：候选 {len(candidates)} 条"
                    f"（已导入 {len(existing_names)} 条）"
                ),
                "data": {
                    "provider_id": provider.provider_id,
                    "type": provider.provider_type,
                    "prefix": prefix or "(none)",
                    "candidate_count": len(candidates),
                    "already_imported_count": len(existing_names),
                    "elapsed_ms": elapsed_ms,
                },
            },
        )
        return candidates


def _load_litellm_cost_map() -> dict[str, object]:
    """惰性读取 litellm 内置模型目录（model cost map）。

    litellm 导入开销大（秒级），故延迟到首次 discover 时加载；返回的
    dict 以 litellm 路由名为键、条目元数据 dict 为值。

    参数:
        无。

    返回:
        litellm 模型目录字典。

    异常:
        透传 litellm 导入 / 读取异常（由调用方归一为 ProviderDiscoverError）。

    副作用:
        首次调用时导入 litellm（进程内后续复用）。
    """

    import litellm

    return dict(litellm.get_model_cost_map(""))
