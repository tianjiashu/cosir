"""模型条目领域服务（models 表 CRUD + discover 批量导入编排）。

单一职责：面向 API 层提供模型条目的增删改查与「按厂商批量导入（勾选入库 /
手动添加）」编排；DB 读写委托 ``ModelEntryCrud``；导入与关键状态变更写
info 审计日志（``models_imported`` 等，设计文档 §7.1）。

职责边界：
- 负责：模型条目读写、批量导入的同厂商去重（已存在同名模型跳过而非报错）、
  导入结果值对象组装。
- 不负责：厂商读写与 Key 状态（``provider_service``）、litellm 目录发现
  （``provider_discover_service``）、解析链（``model_resolver_service``）。
"""

from dataclasses import dataclass, field

from app.config.logging.logger import log
from app.models import ModelEntryRecord
from app.service import depends as service_depends
from app.storage.crud.model_entry_crud import ModelEntryCrud


@dataclass
class ModelImportResult:
    """一次批量导入的结果值对象。

    属性:
        provider_id: 导入目标厂商。
        imported: 成功落库的条目列表。
        skipped_model_names: 被跳过的模型名列表（该厂商下已存在同名条目）。
    """

    provider_id: str
    imported: list[ModelEntryRecord] = field(default_factory=list)
    skipped_model_names: list[str] = field(default_factory=list)


class ModelEntryService:
    """模型条目 CRUD 与批量导入服务。"""

    def __init__(self) -> None:
        """初始化模型条目 service。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 ModelEntryCrud 单例并保存引用。
        """

        self._model_entry_crud: ModelEntryCrud = service_depends.get_model_entry_crud()

    def list_models(self, enabled: bool | None = None) -> list[ModelEntryRecord]:
        """列出全部启用模型条目，按厂商、排序权重、创建时间升序。

        参数:
            无。

        返回:
            全部模型条目列表（含禁用项；启用过滤由 API 投影决定）。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        return self._model_entry_crud.list_all(enabled)

    def list_models_by_provider(self, provider_id: str) -> list[ModelEntryRecord]:
        """列出某厂商下全部模型条目。

        参数:
            provider_id: 厂商标识。

        返回:
            该厂商的模型条目列表；厂商无模型时为空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果查询失败。

        副作用:
            打开一次主库只读 session。
        """

        return self._model_entry_crud.list_by_provider(provider_id)

    def get_model(self, model_id: str) -> ModelEntryRecord:
        """按标识返回单个模型条目。

        参数:
            model_id: 模型条目标识。

        返回:
            匹配的 ``ModelEntryRecord``。

        异常:
            KeyError: 如果指定条目不存在。

        副作用:
            打开一次主库只读 session。
        """

        return self._model_entry_crud.get(model_id)

    def import_models(
        self,
        provider_id: str,
        entries: list[dict[str, object]],
    ) -> ModelImportResult:
        """按厂商批量导入模型条目（discover 勾选 / 手动添加统一入口）。

        去重语义：该厂商下已存在的 ``model_name`` 跳过（计入
        ``skipped_model_names``）而非报错，保证 discover 候选「二次勾选已导入
        项」是安全的幂等操作；其余条目单事务写入，任一违约整批回滚。

        参数:
            provider_id: 导入目标厂商标识。
            entries: 待导入条目的字段字典列表（字段与 ``ModelEntryCrud.create``
                的关键字参数一致，不含 provider_id；为空时返回空结果）。

        返回:
            ``ModelImportResult``（成功导入条目 + 跳过的重名模型）。

        异常:
            ValueError: 如果任一条目字段非法（空白 / 窗口非正）。
            sqlalchemy.exc.IntegrityError: 如果厂商不存在（FK 违约）或批内
                存在重名。
            sqlalchemy.exc.SQLAlchemyError: 如果写入失败。

        副作用:
            向 ``models`` 表批量插入行；写 info 日志 ``models_imported``。
        """

        existing_names = {
            record.model_name for record in self._model_entry_crud.list_by_provider(provider_id)
        }
        pending: list[dict[str, object]] = []
        skipped: list[str] = []
        seen_in_batch: set[str] = set()
        for entry in entries:
            model_name = str(entry.get("model_name", "")).strip()
            if model_name in existing_names or model_name in seen_in_batch:
                skipped.append(model_name)
                continue
            seen_in_batch.add(model_name)
            item = dict(entry)
            item["provider_id"] = provider_id
            pending.append(item)

        imported = self._model_entry_crud.bulk_create(pending)
        log.info(
            "models_imported",
            extra={
                "msg": (
                    f"模型条目批量导入完成：新增 {len(imported)} 条，"
                    f"跳过 {len(skipped)} 条（已存在）"
                ),
                "data": {
                    "provider_id": provider_id,
                    "imported_count": len(imported),
                    "imported_model_names": [record.model_name for record in imported],
                    "skipped_model_names": skipped,
                },
            },
        )
        return ModelImportResult(
            provider_id=provider_id,
            imported=imported,
            skipped_model_names=skipped,
        )

    def update_model(
        self,
        model_id: str,
        *,
        display_name: str | None = None,
        max_context_window: int | None = None,
        supports_thinking: bool | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> ModelEntryRecord:
        """更新模型条目字段并写 ``model_updated`` 审计日志。

        参数:
            model_id: 模型条目标识。
            display_name: 可选，新展示名。
            max_context_window: 可选，新上下文窗口。
            supports_thinking: 可选，新推理模型标识。
            enabled: 可选，新启用状态（下拉只显示启用项）。
            sort_order: 可选，新排序权重。

        返回:
            更新后的 ``ModelEntryRecord``。

        异常:
            KeyError: 如果指定条目不存在。
            ValueError: 如果字段非法。
            sqlalchemy.exc.SQLAlchemyError: 如果更新失败。

        副作用:
            更新 ``models`` 表对应行；写 info 日志 ``model_updated``。
        """

        record = self._model_entry_crud.update(
            model_id,
            display_name=display_name,
            max_context_window=max_context_window,
            supports_thinking=supports_thinking,
            enabled=enabled,
            sort_order=sort_order,
        )
        log.info(
            "model_updated",
            extra={
                "msg": f"模型条目已更新：{record.model_name}",
                "data": {
                    "model_id": model_id,
                    "provider_id": record.provider_id,
                    "enabled": record.enabled,
                },
            },
        )
        return record

    def delete_model(self, model_id: str) -> None:
        """删除单个模型条目并写 ``model_deleted`` 审计日志。

        参数:
            model_id: 模型条目标识。

        返回:
            无。

        异常:
            无。条目不存在时静默无操作（幂等删除语义）。

        副作用:
            从 ``models`` 表删除匹配行；写 info 日志 ``model_deleted``。
        """

        try:
            record = self._model_entry_crud.get(model_id)
        except KeyError:
            record = None
        self._model_entry_crud.delete(model_id)
        log.info(
            "model_deleted",
            extra={
                "msg": "模型条目已删除",
                "data": {
                    "model_id": model_id,
                    "model_name": record.model_name if record else None,
                    "provider_id": record.provider_id if record else None,
                },
            },
        )
