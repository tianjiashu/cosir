"""模型连接配置领域服务。"""

from time import perf_counter

from app.config.constant import Constant
from app.config.logging.logger import log
from app.models.model_config_record import ModelConfigRecord
from app.service import depends as service_depends
from app.service.model_config.connection_test_result import ModelConfigTestResult
from app.storage.crud.model_config_crud import ModelConfigCrud
from app.utils.http_proxy import build_proxy_async_client


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

    async def test_connection(self, record: ModelConfigRecord) -> ModelConfigTestResult:
        """使用配置中的真实 URL、Key 和模型名发送最小 chat 请求。"""

        started = perf_counter()
        try:
            await self._acompletion_ping(
                model=record.model_name,
                base_url=record.base_url,
                api_key=record.api_key,
            )
        except Exception:
            elapsed_ms = int((perf_counter() - started) * 1000)
            log.exception(
                "model_config_connection_test_failed",
                extra={
                    "msg": "模型连接测试失败",
                    "data": {"config_id": record.id, "elapsed_ms": elapsed_ms},
                },
            )
            return ModelConfigTestResult(
                config_id=record.id,
                success=False,
                elapsed_ms=elapsed_ms,
                error_code="connection_failed",
                error_message="连接失败，请检查 Base URL、API Key 和模型名称",
            )

        elapsed_ms = int((perf_counter() - started) * 1000)
        log.info(
            "model_config_connection_test_succeeded",
            extra={
                "msg": "模型连接测试成功",
                "data": {"config_id": record.id, "elapsed_ms": elapsed_ms},
            },
        )
        return ModelConfigTestResult(record.id, True, elapsed_ms)

    async def test_draft(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
    ) -> ModelConfigTestResult:
        """测试尚未保存的表单配置，不写数据库。"""

        started = perf_counter()
        try:
            await self._acompletion_ping(model=model_name, base_url=base_url, api_key=api_key)
        except Exception:
            elapsed_ms = int((perf_counter() - started) * 1000)
            log.exception(
                "model_config_draft_test_failed",
                extra={"msg": "未保存模型配置测试失败", "data": {"elapsed_ms": elapsed_ms}},
            )
            return ModelConfigTestResult(
                None, False, elapsed_ms, "connection_failed", "连接失败，请检查配置"
            )
        return ModelConfigTestResult(None, True, int((perf_counter() - started) * 1000))

    @staticmethod
    async def _acompletion_ping(*, model: str, base_url: str, api_key: str) -> None:
        """向 OpenAI 兼容 ``/chat/completions`` 发送最小请求。"""

        if not base_url.strip():
            raise ValueError("base_url must not be blank")
        if not model.strip():
            raise ValueError("model_name must not be blank")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with build_proxy_async_client(timeout=Constant.LLM.REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                },
            )
            response.raise_for_status()
