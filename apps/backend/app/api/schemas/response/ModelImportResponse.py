from pydantic import BaseModel

from app.api.schemas.response.ModelEntryResponse import ModelEntryResponse
from app.models.model_entry_record import ModelEntryRecord
from app.models.provider_record import ProviderRecord
from app.llm_provider.provider import ModelImportResult


class ModelImportResponse(BaseModel):
    """序列化模型批量导入结果响应。

    参数:
        provider_id: 导入目标厂商标识。
        imported: 成功导入的条目列表（含厂商聚合信息，供前端即时刷新展示）。
        skipped_model_names: 被跳过的模型名列表（该厂商下已存在同名条目）。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    provider_id: str
    imported: list[ModelEntryResponse]
    skipped_model_names: list[str]

    @classmethod
    def from_result(
        cls,
        result: ModelImportResult,
        provider: ProviderRecord,
        *,
        api_key_configured: bool,
    ) -> "ModelImportResponse":
        """从 service 导入结果值对象构造响应模型。

        参数:
            result: ``ModelEntryService.import_models`` 的结果值对象。
            provider: 导入目标厂商记录（为导入条目提供厂商聚合信息）。
            api_key_configured: 厂商 Key 配置状态（透传给条目响应）。

        返回:
            与导入结果对齐的 ``ModelImportResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        def _to_response(record: ModelEntryRecord) -> ModelEntryResponse:
            return ModelEntryResponse.from_records(
                record, provider, api_key_configured=api_key_configured
            )

        return cls(
            provider_id=result.provider_id,
            imported=[_to_response(record) for record in result.imported],
            skipped_model_names=list(result.skipped_model_names),
        )
