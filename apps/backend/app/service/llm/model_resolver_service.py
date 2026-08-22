"""模型解析服务：模型名 → ``LLMRuntimeConfig`` 的两段式解析核心。

单一职责：以 DB（``providers`` / ``models`` 表）为唯一事实来源，把「请求的
模型名（或 ``None`` 表示未选择模型）」解析为可直接构建 chat model 的
``LLMRuntimeConfig``；未命中抛 ``ModelNotConfiguredError``。

两段式消费方（设计文档 §6）：
- ① service 创建期预解析（``turn_service.create_turn`` /
  ``task_service.create_task_with_initial_turn`` / ``delegation_executor``）：
  失败 → HTTP 422（child 无 HTTP 上下文 → delegation 置 failed）；
- ② workflow 运行期兜底解析（``ReactLikeWorkflow.run``）：失败 → RUN_FAILED
  终态。两段共用本服务的 ``resolve``，仅错误出口不同。

职责边界：
- 负责：``requested_model=None`` 早返回拒绝、启用模型命中查询、归属厂商
  启用 / Key 存在性检查、``LLMRuntimeConfig`` 组装（含 Key 明文透传）。
- 不负责：Key 的实际落库与生命周期（唯一事实源是 ``providers.api_key`` 列，
  见 ``provider_service``）、上下文窗口 min 收敛（``context_window_resolver``）。

2026-08-18 决议落地（设计文档阶段 1.5）：
- ``ModelNotConfiguredError`` 扩展 ``error_code`` 属性，与 ``ErrorKind``
  枚举对齐，供 ``RunFailedPayload.error_code`` 透传给前端区分失败性质；
- 新增 ``REASON_MODEL_NOT_SELECTED`` 原因码，``requested_model=None`` 时
  早返回拒绝（不再透传 None 给 ``find_enabled_by_name`` 触发 SQL 层错误）；
- 不再创建独立的 ``core/llm/model_error_mapper.py`` 重复定义同名异常类
  （违反「不重复造轮子」铁律），改为在现有异常类上扩展 ``error_code``。
"""

from app.config.logging.logger import log
from app.models import LLMRuntimeConfig
from app.models.enums.error_kind import ErrorKind
from app.service import depends as service_depends
from app.service.provider.provider_service import ProviderService
from app.storage.crud.model_entry_crud import ModelEntryCrud

# 拒绝原因短码：模型未收录于 DB（启用模型查不到）。
REASON_MODEL_NOT_FOUND = "model_not_found"
# 拒绝原因短码：模型命中但归属厂商被禁用。
REASON_PROVIDER_DISABLED = "provider_disabled"
# 拒绝原因短码：厂商需要 Key 但 providers.api_key 为空。
REASON_API_KEY_MISSING = "api_key_missing"
# 拒绝原因短码：请求模型名为 None（设计阶段 1.5 引入）。
# 用户决议「5 个内置 profile 不内置默认模型」后，前端未选模型 / 后端
# ``agent.model_name is None`` 均走本原因码，前端据此显示「请先选择模型」
# 引导。早返回避免 None 透传到 ``find_enabled_by_name`` 触发 SQL 层错误。
REASON_MODEL_NOT_SELECTED = "model_not_selected"

# 修复指引文案（按拒绝原因区分，见设计文档 §8.4）。
_GUIDANCE_BY_REASON = {
    REASON_MODEL_NOT_FOUND: "请前往配置中心添加/导入该模型，或在下拉中选择已配置的模型",
    REASON_PROVIDER_DISABLED: "请前往配置中心启用该模型所属厂商",
    REASON_API_KEY_MISSING: "请前往配置中心为该厂商填写 API Key，或选择无需 Key 的模型",
    REASON_MODEL_NOT_SELECTED: "请先在对话窗口选择模型后再发送消息",
}


class ModelNotConfiguredError(Exception):
    """请求的模型不可用（未选择 / 未收录 / 厂商禁用 / Key 未配置）。

    属性:
        model_name: 被拒绝的模型名（``REASON_MODEL_NOT_SELECTED`` 时为 ``"(none)"``）。
        reason: 拒绝原因短码（``model_not_selected`` / ``model_not_found`` /
            ``provider_disabled`` / ``api_key_missing``）。
        guidance: 面向用户的修复指引文案。
        error_code: 与 ``ErrorKind`` 枚举对齐的稳定错误码，供
            ``RunFailedPayload.error_code`` 透传给前端区分失败性质
            （当前恒为 ``ErrorKind.MODEL_NOT_CONFIGURED.value``）。
    """

    #: 与 ``ErrorKind`` 枚举对齐的稳定错误码（设计文档阶段 1.5）。
    error_code: str = ErrorKind.MODEL_NOT_CONFIGURED.value

    def __init__(self, model_name: str, reason: str) -> None:
        """初始化模型未配置错误。

        参数:
            model_name: 被拒绝的模型名（``REASON_MODEL_NOT_SELECTED`` 时调用方
                传入 ``"(none)"`` 占位，避免日志空字段）。
            reason: 拒绝原因短码。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """

        self.model_name = model_name
        self.reason = reason
        self.guidance = _GUIDANCE_BY_REASON.get(reason, "请检查模型配置")
        super().__init__(f"model {model_name} not configured: {reason}; {self.guidance}")


class ModelResolverService:
    """以 DB 为唯一事实来源的模型解析服务（两段式解析共用核心）。"""

    def __init__(self) -> None:
        """初始化解析服务。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 ModelEntryCrud 单例与 ProviderService
            单例并保存引用。
        """

        self._model_entry_crud: ModelEntryCrud = service_depends.get_model_entry_crud()
        self._provider_service: ProviderService = service_depends.get_provider_service()

    def resolve(
        self,
        requested_model: str | None,
        *,
        child: bool = False,
        log_context: dict[str, object] | None = None,
    ) -> LLMRuntimeConfig:
        """把请求模型名（``None`` 表示未选择）解析为运行时配置。

        解析步骤：``requested_model is None`` 早返回拒绝
        （``REASON_MODEL_NOT_SELECTED``，设计文档阶段 1.5）→ 按名查启用模型行
        → 查归属厂商并检查启用 → Key 存在性检查 → 组装 ``LLMRuntimeConfig``。
        任一步失败抛 ``ModelNotConfiguredError`` 并写 warn 级
        ``model_resolve_rejected`` 日志（child 路径 data 标注 ``child=true``，
        设计文档 §6.4）。

        参数:
            requested_model: 前端 / 委派请求的模型名；``None`` 表示用户未选择
                模型（前端优先校验失败后的后端兜底）。
            child: 是否为 child 委派路径（仅影响日志 data 标注）。
            log_context: 可选附加日志上下文（如 task_id / turn_id）。

        返回:
            解析成功的 ``LLMRuntimeConfig``。

        异常:
            ModelNotConfiguredError: 模型未选择 / 未收录 / 厂商禁用 / Key 未配置。

        副作用:
            打开主库只读 session；拒绝时写 warn 日志。
        """

        # 防御性早返回：避免 None 透传到 ``find_enabled_by_name(model_name: str)``
        # 触发 mypy / 运行时 None 比较异常。前端未选模型时直接走本分支。
        if requested_model is None:
            return self._reject(
                "(none)",
                REASON_MODEL_NOT_SELECTED,
                self._build_context(None, child, log_context),
            )

        context_data = self._build_context(requested_model, child, log_context)

        model_entry = self._model_entry_crud.find_enabled_by_name(requested_model)
        if model_entry is None:
            return self._reject(requested_model, REASON_MODEL_NOT_FOUND, context_data)

        provider = self._provider_service.get_provider(model_entry.provider_id)
        if provider is None or not provider.enabled:
            return self._reject(requested_model, REASON_PROVIDER_DISABLED, context_data)

        if not self._provider_service.api_key_configured(provider):
            return self._reject(requested_model, REASON_API_KEY_MISSING, context_data)

        return LLMRuntimeConfig.from_model_entry(provider, model_entry)

    @staticmethod
    def _build_context(
        requested_model: str | None,
        child: bool,
        log_context: dict[str, object] | None,
    ) -> dict[str, object]:
        """组装解析日志上下文。

        参数:
            requested_model: 请求的模型名（``None`` 时日志记录为 ``None``）。
            child: 是否 child 委派路径。
            log_context: 调用方附加上下文（如 task_id / turn_id）。

        返回:
            含 ``model`` / ``child`` 与调用方附加字段的上下文字典。

        异常:
            无。

        副作用:
            无。
        """

        context_data: dict[str, object] = {"model": requested_model, "child": child}
        if log_context:
            context_data.update(log_context)
        return context_data

    def _reject(
        self,
        model_name: str,
        reason: str,
        context_data: dict[str, object],
    ) -> LLMRuntimeConfig:
        """记录拒绝日志并抛出 ``ModelNotConfiguredError``。

        参数:
            model_name: 被拒绝的模型名。
            reason: 拒绝原因短码。
            context_data: 日志 data 上下文（含 model / child 等）。

        返回:
            无（恒抛出）。

        异常:
            ModelNotConfiguredError: 恒定抛出。

        副作用:
            写 warn 级 ``model_resolve_rejected`` 日志。
        """

        data = dict(context_data)
        data["reason"] = reason
        log.warning(
            "model_resolve_rejected",
            extra={
                "msg": f"模型解析被拒绝，model={model_name}，reason={reason}",
                "data": data,
            },
        )
        raise ModelNotConfiguredError(model_name, reason)
