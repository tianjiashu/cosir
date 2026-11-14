"""模型配置领域服务：连接与其下模型，事实源是模型配置文件。"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import ValidationError

from app.config.logging.logger import log
from app.core.agents.resolved_model import ResolvedModel, resolve_model
from app.core.llm_provider.model_probe import (
    ModelProbeFailureKind,
    ModelProbeOutcome,
    probe_chat_model,
)
from app.models.enums.reasoning_effort import ReasoningEffort
from app.models.errors.input_errors import UserInputRejectedError
from app.models.model_config_file import ModelConfigFile, ModelConnectionEntry, ModelEntry
from app.models.model_config_record import ModelConfigRecord
from app.service.configuration.file_store import ConfigurationFileStore
from app.service.model_config.connection_test_result import ModelConfigTestResult
from app.service.model_config.probe_failure_messages import probe_failure_message
from app.utils.path.system_cosir import system_cosir_dir, system_model_config_file


class ModelConfigFileError(ValueError):
    """模型配置文件无法解析或违反契约；消息指出出错位置，可直接展示给用户。"""


class ModelConfigService:
    """读写模型配置文件，并对未保存的「连接 + 模型」做真实连接测试。

    每次调用都现读文件（文件很小），手改后立即生效，没有缓存也就没有失效问题。写入在文件锁内
    「读出 → 修改 → 原子写回」；文件已经写坏时拒绝一切写入，不覆盖用户手改的内容。
    重名在写入边界以 ``FileExistsError`` 报告（映射 409）；手改文件里的重名由读取校验报告。
    「测试通过才能保存」由界面按模型把关，本服务的写入不复测，手改文件同样不经测试。
    写入的字段由 API 边界校验过，这里用 ``model_copy`` 合并，不再重复字段校验。
    """

    def __init__(self) -> None:
        self._store = ConfigurationFileStore()

    @property
    def path(self) -> Path:
        return system_model_config_file()

    def read(self) -> ModelConfigFile:
        """读取并校验配置文件；还没有文件时视为没有连接。

        异常:
            ModelConfigFileError: JSON 无效或违反契约，消息带出错位置。
            ConfigurationFileError: 文件存在但无法读取。
        """

        if not self.path.exists():
            return ModelConfigFile()
        content = self._store.read_text(self.path, root=system_cosir_dir())
        try:
            return ModelConfigFile.model_validate_json(content)
        except ValidationError as exc:
            message = f"模型配置文件 {self.path} 无效：{_describe(exc)}"
            log.error(
                "model_config_file_invalid",
                extra={"msg": message, "data": {"path": str(self.path)}},
            )
            raise ModelConfigFileError(message) from exc

    def list_configs(self) -> list[ModelConfigRecord]:
        """按文件顺序列出全部模型（连接顺序，连接内按加入顺序）。"""

        return [
            ModelConfigRecord(connection, model)
            for connection in self.read().connections
            for model in connection.models
        ]

    def get_config(self, config_id: str) -> ModelConfigRecord:
        """按模型 id 读取，不存在时抛 ``KeyError``。"""

        return ModelConfigRecord(*self.read().find(config_id))

    def exists(self, config_id: str) -> bool:
        """模型是否还在文件里；文件写坏时也视为不可用（调用方据此不开新 Run）。"""

        try:
            self.get_config(config_id)
        except (KeyError, ModelConfigFileError):
            return False
        return True

    def check_launch_config(
        self, model_config_id: str, reasoning_effort: ReasoningEffort | None
    ) -> ModelConfigRecord:
        """校验模型存在且支持所选推理强度，返回它供调用方继续校验能力（如图片）。

        异常:
            UserInputRejectedError: 模型不存在、配置文件无效，或不支持所选推理强度。
        """
        # 模型缺失不是「寻址不到 Session」：接入层把 KeyError 统一映射为 404 session not found。
        # 本层明确知道自己查的是什么，就地换成可读的输入拒绝，避免给出错误语义。
        try:
            config = self.get_config(model_config_id)
        except KeyError as exc:
            raise UserInputRejectedError(
                "MODEL_CONFIG_UNAVAILABLE",
                "当前模型配置不存在，请重新选择模型",
                detail=f"model config {model_config_id} is gone",
            ) from exc
        except ModelConfigFileError as exc:
            raise UserInputRejectedError("MODEL_CONFIG_FILE_INVALID", str(exc)) from exc
        if reasoning_effort is not None and not config.model.supports_reasoning_effort:
            raise UserInputRejectedError(
                "REASONING_EFFORT_UNSUPPORTED", "当前模型不支持所选的推理强度"
            )
        return config

    def create_connection(
        self, *, name: str, base_url: str, api_key: str, models: Sequence[ModelEntry]
    ) -> ModelConnectionEntry:
        """新建连接与它的首批模型（一次写入）；连接 id 由名称生成，之后不随改名变化。

        异常:
            FileExistsError: 连接名称已存在。
        """

        created: list[ModelConnectionEntry] = []

        def change(current: ModelConfigFile) -> tuple[ModelConnectionEntry, ...]:
            if any(connection.name == name for connection in current.connections):
                raise FileExistsError("连接名称已存在")
            connection = ModelConnectionEntry(
                id=current.new_connection_id(name),
                name=name,
                base_url=base_url,
                api_key=api_key,
                models=tuple(models),
            )
            created.append(connection)
            return (*current.connections, connection)

        self._write(change)
        log.info(
            "model_connection_created",
            extra={
                "msg": "模型连接已创建",
                "data": {
                    "connection_id": created[0].id,
                    "models": [model.model_name for model in models],
                },
            },
        )
        return created[0]

    def update_connection(self, connection_id: str, **changes: str) -> ModelConnectionEntry:
        """更新连接的名称、地址或 Key；记录修改了哪些字段，不记录 Key 内容。

        异常:
            KeyError: 连接不存在。
            FileExistsError: 新名称被其他连接占用。
        """

        def change(current: ModelConfigFile) -> tuple[ModelConnectionEntry, ...]:
            target = current.connection(connection_id)
            name = changes.get("name")
            if name and any(c.name == name and c.id != connection_id for c in current.connections):
                raise FileExistsError("连接名称已存在")
            updated = target.model_copy(update=changes)
            return tuple(updated if c.id == connection_id else c for c in current.connections)

        written = self._write(change)
        log.info(
            "model_connection_updated",
            extra={
                "msg": "模型连接已更新",
                "data": {"connection_id": connection_id, "updated_fields": sorted(changes)},
            },
        )
        return written.connection(connection_id)

    def delete_connection(self, connection_id: str) -> None:
        """删除连接及其全部模型；连接已不在时什么也不做。引用这些模型的历史 Run 保留原 id。"""

        self._write(lambda current: tuple(c for c in current.connections if c.id != connection_id))
        log.info(
            "model_connection_deleted",
            extra={"msg": "模型连接已删除", "data": {"connection_id": connection_id}},
        )

    def add_models(self, connection_id: str, models: Sequence[ModelEntry]) -> ModelConnectionEntry:
        """给已有连接追加一批模型（一次写入，任何一个重名则整批不写）。

        异常:
            KeyError: 连接不存在。
            FileExistsError: 这批里有模型已在该连接下。
        """

        def change(current: ModelConfigFile) -> tuple[ModelConnectionEntry, ...]:
            target = current.connection(connection_id)
            existing = {model.model_name for model in target.models}
            duplicated = [model.model_name for model in models if model.model_name in existing]
            if duplicated:
                raise FileExistsError(f"该连接下已有同名模型：{'、'.join(duplicated)}")
            updated = target.model_copy(update={"models": (*target.models, *models)})
            return tuple(updated if c.id == connection_id else c for c in current.connections)

        written = self._write(change)
        log.info(
            "model_configs_added",
            extra={
                "msg": "连接下已追加模型",
                "data": {
                    "connection_id": connection_id,
                    "models": [model.model_name for model in models],
                },
            },
        )
        return written.connection(connection_id)

    def update_model(
        self, connection_id: str, model_name: str, **changes: object
    ) -> ModelConfigRecord:
        """更新模型的上下文窗口或能力声明；模型名不可改（改名会让引用它的对话与 Agent 换模型）。

        异常:
            KeyError: 连接或模型不存在。
        """

        def change(current: ModelConfigFile) -> tuple[ModelConnectionEntry, ...]:
            target = current.connection(connection_id)
            if not any(model.model_name == model_name for model in target.models):
                raise KeyError(model_name)
            models = tuple(
                model.model_copy(update=changes) if model.model_name == model_name else model
                for model in target.models
            )
            updated = target.model_copy(update={"models": models})
            return tuple(updated if c.id == connection_id else c for c in current.connections)

        written = self._write(change)
        log.info(
            "model_config_updated",
            extra={
                "msg": "模型配置已更新",
                "data": {
                    "connection_id": connection_id,
                    "model_name": model_name,
                    "updated_fields": sorted(changes),
                },
            },
        )
        return ModelConfigRecord(*written.find(f"{connection_id}/{model_name}"))

    def delete_model(self, connection_id: str, model_name: str) -> None:
        """删除一个模型；已不在时什么也不做。连接保留，引用它的历史 Run 保留原 id。"""

        def change(current: ModelConfigFile) -> tuple[ModelConnectionEntry, ...]:
            return tuple(
                c.model_copy(
                    update={"models": tuple(m for m in c.models if m.model_name != model_name)}
                )
                if c.id == connection_id
                else c
                for c in current.connections
            )

        self._write(change)
        log.info(
            "model_config_deleted",
            extra={
                "msg": "模型配置已删除",
                "data": {"connection_id": connection_id, "model_name": model_name},
            },
        )

    def _write(
        self, change: Callable[[ModelConfigFile], tuple[ModelConnectionEntry, ...]]
    ) -> ModelConfigFile:
        """在文件锁内读出、按 ``change`` 得到新的连接列表、整体校验后原子写回。"""

        with self._store.locked(self.path):
            # 重新构造而不是 model_copy：要让整份文件再过一遍唯一性校验。
            written = ModelConfigFile(connections=change(self.read()))
            content = json.dumps(written.model_dump(mode="json"), ensure_ascii=False, indent=2)
            self._store.write_text_atomic(self.path, content + "\n", root=system_cosir_dir())
        return written

    async def test_draft(
        self, *, base_url: str, api_key: str, model: ModelEntry
    ) -> ModelConfigTestResult:
        """对一个尚未保存的「连接 + 模型」执行一次真实探活，不写文件。

        草稿经与运行期相同的 ``resolve_model`` 解析为 ``ResolvedModel``，再复用统一的流式探活
        入口，避免测试成功与实际运行使用两套请求逻辑。能力声明会改变请求参数（如推理强度），
        所以测试的是「这个模型按这组能力」能否调用；批量添加时前端对每个模型各调一次。

        返回:
            ``ModelConfigTestResult``；成功表示至少收到一个模型生成 chunk，失败携带稳定错误分类和
            受控文案。不抛异常。

        副作用:
            发起一次真实模型请求；失败时写 ``model_config_connection_test_failed`` warning 日志。
        """

        settings = resolve_model(
            ModelConfigRecord(
                # 草稿连接没有身份：探活只读地址与 Key。
                ModelConnectionEntry(id="draft", name="draft", base_url=base_url, api_key=api_key),
                model,
            )
        )
        outcome = await probe_chat_model(settings)
        return self._outcome_result(settings, outcome)

    def _outcome_result(
        self, settings: ResolvedModel, outcome: ModelProbeOutcome
    ) -> ModelConfigTestResult:
        """把探活结果映射为连接测试结果，并记录成功 / 失败日志。

        参数:
            settings: 本次探活使用的运行设置（仅用于日志中的 model / base_url）。
            outcome: 探活结果。

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
                        "model": settings.model_name,
                        "base_url": settings.base_url,
                        "elapsed_ms": outcome.elapsed_ms,
                    },
                },
            )
            return ModelConfigTestResult(
                success=True,
                elapsed_ms=outcome.elapsed_ms,
            )
        kind = outcome.failure_kind or ModelProbeFailureKind.UNKNOWN
        log.warning(
            "model_config_connection_test_failed",
            extra={
                "msg": "模型连接测试失败",
                "data": {
                    "model": settings.model_name,
                    "base_url": settings.base_url,
                    "failure_kind": kind.value,
                    "detail": outcome.detail,
                    "elapsed_ms": outcome.elapsed_ms,
                },
            },
        )
        return self._failure_result(kind, outcome.elapsed_ms)

    @staticmethod
    def _failure_result(kind: ModelProbeFailureKind, elapsed_ms: int) -> ModelConfigTestResult:
        """按失败分类构造结果（文案取自受控文案目录，不携带原始异常正文）。"""

        return ModelConfigTestResult(
            success=False,
            elapsed_ms=elapsed_ms,
            error_code=kind.value,
            error_message=probe_failure_message(kind),
        )


def _describe(exc: ValidationError) -> str:
    """取第一处错误，写成「connections[1].models[0].context_window_k：原因」。"""

    error = exc.errors()[0]
    location = "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"]
    )
    return f"{location.lstrip('.')}：{error['msg']}" if location else error["msg"]
