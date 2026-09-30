"""模型连接配置领域服务。"""

from app.config.logging.logger import log
from app.core.agents.model_settings import ModelSettings
from app.core.llm_provider.model_probe import (
    ModelProbeFailureKind,
    ModelProbeOutcome,
    probe_chat_model,
)
from app.models.model_config_record import ModelConfigRecord
from app.service import depends as service_depends
from app.service.model_config.connection_test_result import ModelConfigTestResult
from app.service.model_config.probe_failure_messages import probe_failure_message
from app.storage.crud.model_config_crud import ModelConfigCrud


class ModelConfigService:
    """编排模型配置 CRUD、Key 状态聚合和真实连接测试。"""

    def __init__(self) -> None:
        """绑定主库模型配置 CRUD。"""

        self._crud: ModelConfigCrud = service_depends.get_model_config_crud()

    def list_configs(self, enabled: bool | None = None) -> list[ModelConfigRecord]:
        """返回模型配置列表。"""

        return self._crud.list_all(enabled)

    def get_config(self, config_id: int) -> ModelConfigRecord:
        """按主键读取模型配置。"""

        return self._crud.get(config_id)

    def create_config(self, **fields: object) -> ModelConfigRecord:
        """创建配置并写入不含密钥的结构化审计日志。"""

        record = self._crud.create(**fields)
        log.info(
            "model_config_created",
            extra={
                "msg": "模型连接配置已创建",
                "data": {"config_id": record.id, "model_name": record.model_name},
            },
        )
        return record

    def update_config(self, config_id: int, **changes: object) -> ModelConfigRecord:
        """更新配置并记录修改字段，不记录密钥内容。"""

        record = self._crud.update(config_id, **changes)
        log.info(
            "model_config_updated",
            extra={
                "msg": "模型连接配置已更新",
                "data": {"config_id": config_id, "updated_fields": sorted(changes)},
            },
        )
        return record

    def delete_config(self, config_id: int) -> None:
        """删除模型配置；历史 Run 保留自身的模型事实快照。"""

        self._crud.delete(config_id)
        log.info(
            "model_config_deleted",
            extra={"msg": "模型连接配置已删除", "data": {"config_id": config_id}},
        )

    @staticmethod
    def api_key_configured(record: ModelConfigRecord) -> bool:
        """返回配置是否有非空 API Key。"""

        return bool(record.api_key)

    async def test_draft(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        context_window_k: int,
        supports_thinking: bool,
        supports_reasoning_effort: bool,
        supports_image: bool,
        config_name: str,
    ) -> ModelConfigTestResult:
        """对完整的未保存表单执行一次真实探活，不写数据库。

        ``config_name`` 只用于保证调用契约与保存表单一致，探活本身不需要读取或记录它。
        其余字段会物化为与保存后运行期一致的 ``ModelSettings``，然后复用统一的流式探活
        入口，避免测试成功与实际运行使用两套请求逻辑。

        参数:
            config_name: 表单中的配置名称，仅用于完整请求契约校验。
            base_url: 表单中的 Base URL。
            api_key: 表单中的 API Key，仅在本次调用链内部传递。
            model_name: 表单中的模型名称。
            context_window_k: 表单中的 K 单位上下文窗口。
            supports_thinking: 表单声明的思考能力。
            supports_reasoning_effort: 表单声明的推理强度能力。
            supports_image: 表单声明的图片输入能力。

        返回:
            ``ModelConfigTestResult``（``config_id`` 恒为 None）；成功表示至少收到一个模型
            生成 chunk，失败携带稳定错误分类和受控文案。

        异常:
            无。

        副作用:
            发起一次真实模型请求；失败时写 ``model_config_connection_test_failed`` warning 日志
            （``source`` 为 ``draft``）。
        """

        # API schema 已经校验了配置名称，服务层仍显式消费该字段，防止调用方绕过完整表单
        # 契约时出现未被发现的参数漂移。
        if not config_name.strip():
            return self._failure_result(None, ModelProbeFailureKind.INVALID_CONFIG, 0)
        settings = ModelSettings(
            base_url=base_url.strip() or None,
            api_key=api_key,
            model_name=model_name.strip() or None,
            context_window_k=context_window_k,
            supports_thinking=supports_thinking,
            supports_reasoning_effort=supports_reasoning_effort,
            supports_image=supports_image,
        )
        outcome = await probe_chat_model(settings)
        return self._outcome_result(None, settings, outcome, source="draft")

    def _outcome_result(
        self,
        config_id: int | None,
        settings: ModelSettings,
        outcome: ModelProbeOutcome,
        *,
        source: str,
    ) -> ModelConfigTestResult:
        """把探活结果映射为连接测试结果，并记录成功 / 失败日志。

        参数:
            config_id: 已保存配置的标识；当前表单测试场景恒为 None。
            settings: 本次探活使用的运行设置（仅用于日志中的 model / base_url）。
            outcome: 探活结果。
            source: 测试来源标识；当前仅使用 ``draft``。

        返回:
            ``ModelConfigTestResult``；失败时 ``error_code`` 为探活失败分类的稳定取值。

        异常:
            无。

        副作用:
            写一条成功 info 或失败 warning 日志（不含 Key）。
        """

        if outcome.ok:
            log.info(
                "model_config_connection_test_succeeded",
                extra={
                    "msg": "模型连接测试成功",
                    "data": {
                        "config_id": config_id,
                        "source": source,
                        "model": settings.model_name,
                        "base_url": settings.base_url,
                        "elapsed_ms": outcome.elapsed_ms,
                    },
                },
            )
            return ModelConfigTestResult(
                config_id=config_id,
                success=True,
                elapsed_ms=outcome.elapsed_ms,
            )
        kind = outcome.failure_kind or ModelProbeFailureKind.UNKNOWN
        log.warning(
            "model_config_connection_test_failed",
            extra={
                "msg": "模型连接测试失败",
                "data": {
                    "config_id": config_id,
                    "source": source,
                    "model": settings.model_name,
                    "base_url": settings.base_url,
                    "failure_kind": kind.value,
                    "detail": outcome.detail,
                    "elapsed_ms": outcome.elapsed_ms,
                },
            },
        )
        return self._failure_result(config_id, kind, outcome.elapsed_ms)

    @staticmethod
    def _failure_result(
        config_id: int | None,
        kind: ModelProbeFailureKind,
        elapsed_ms: int,
    ) -> ModelConfigTestResult:
        """按失败分类构造结果（文案取自受控文案目录，不携带原始异常正文）。"""

        return ModelConfigTestResult(
            config_id=config_id,
            success=False,
            elapsed_ms=elapsed_ms,
            error_code=kind.value,
            error_message=probe_failure_message(kind),
        )
