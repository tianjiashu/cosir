"""系统级 env 配置 service。

只允许编辑 Settings 已声明的白名单字段。读取结果同时表达磁盘值、进程有效值和来源，Secret
只返回配置状态，不返回原文。配置中心与手写配置共用同一个系统文件（``<数据根>/.cosir/.env``）：
写入只重写本白名单内的键，注释、未知键与未被管理的行为原样保留。

白名单字段目录与字段、分组、变更意图的值对象定义在 ``app.models.environment``，本模块只消费它们，
不承担字段登记职责。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.config.environment_field_catalog import (
    ENVIRONMENT_FIELDS,
    ENVIRONMENT_GROUPS,
)
from app.config.logging.logger import log
from app.config.settings import Settings
from app.models.environment.environment_change import EnvironmentChange
from app.models.environment.environment_field import EnvironmentField
from app.service.configuration.file_store import ConfigurationFileStore
from app.utils.path.system_cosir import system_cosir_dir, system_env_file


class EnvironmentConfigurationError(ValueError):
    """env 配置字段、类型或操作不符合白名单契约。"""


# ``.env`` 的行内 ``KEY=`` 语法只服务于本模块的写回逻辑，属该文件的持久化格式细节，
# 因此不随白名单字段目录一起移入 ``app.models``。
_ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


class EnvironmentConfigurationService:
    """读取和更新 `.env` 白名单字段，并按调用方要求重载运行时配置。"""

    def __init__(self) -> None:
        """初始化系统环境配置 service。

        env 文件位置唯一由 ``app.utils.path`` 决定（``<数据根>/.cosir/.env``），因此不接受
        路径参数，避免出现第二套位置口径；测试若要隔离文件系统，应替换本模块引用的
        ``system_env_file``。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            解析系统 ``.cosir`` 目录与 env 文件路径；不读取文件内容。
        """

        self.env_file = system_env_file()
        self.root = system_cosir_dir()
        self.store = ConfigurationFileStore()

    def read(self) -> list[dict[str, Any]]:
        """返回白名单字段的脱敏来源快照。

        返回值包含字段的展示元数据和当前状态，但不返回 secret 明文。分组投影由
        ``read_grouped`` 完成；这样 service 的值读取测试仍可按字段检查，而 API 只暴露完整的
        分组契约。
        """

        file_values = self._read_values(self.env_file)
        result: list[dict[str, Any]] = []
        for field in ENVIRONMENT_FIELDS.values():
            process_value = Settings.__dict__.get(field.name, field.default)
            process_override = os.environ.get(
                field.name
            ) is not None and not Settings.is_file_loaded_value(field.name)
            if process_override:
                source = "process"
            elif field.name in file_values:
                source = "file"
            else:
                source = "default"
            disk_value = file_values.get(field.name, field.default)
            options = list(field.options)
            if field.component == "select" and not field.secret and process_value is not None:
                process_text = str(process_value)
                if process_text not in {value for value, _ in options}:
                    options.append((process_text, f"当前值（{process_text}）"))
            result.append(
                {
                    "name": field.name,
                    "type": field.value_type,
                    "component": field.component,
                    "group_id": field.group_id,
                    "label": field.label,
                    "secret": field.secret,
                    "default": None if field.secret else field.default,
                    "value": None if field.secret else process_value,
                    "disk_value": None if field.secret else disk_value,
                    "configured": process_value is not None and str(process_value) != "",
                    "masked": field.secret and disk_value is not None and str(disk_value) != "",
                    "source": source,
                    "process_override": process_override,
                    "options": [{"value": value, "label": label} for value, label in options],
                    "placeholder": field.placeholder,
                    "clearable": field.clearable,
                    "minimum": field.minimum,
                    "maximum": field.maximum,
                }
            )
        return result

    def read_grouped(self) -> list[dict[str, Any]]:
        """返回供配置中心 API 使用的分组字段快照。

        分组顺序和字段顺序由后端注册表决定，前端只负责渲染，不需要复制字段分类规则。
        未注册到分组表的字段不会被静默丢弃，而是触发配置契约错误，避免新增字段后 UI 无法
        展示却仍可被写入。
        """

        fields_by_group = {group.id: [] for group in ENVIRONMENT_GROUPS}
        for field in self.read():
            group_id = field["group_id"]
            if group_id not in fields_by_group:
                raise EnvironmentConfigurationError(f"环境配置字段未注册分组: {field['name']}")
            fields_by_group[group_id].append(
                {key: value for key, value in field.items() if key != "group_id"}
            )
        return [
            {
                "id": group.id,
                "label": group.label,
                "description": group.description,
                "fields": fields_by_group[group.id],
            }
            for group in ENVIRONMENT_GROUPS
            if fields_by_group[group.id]
        ]

    def update(
        self,
        changes: Mapping[str, EnvironmentChange],
        *,
        reload_after_write: bool = False,
    ) -> list[dict[str, Any]]:
        """校验并更新 `.env`，可选地重载当前进程配置后返回脱敏快照。

        参数:
            changes: 字段名到变更意图的映射；未出现的字段保持原值。
            reload_after_write: 为 ``True`` 时，原子写入成功后调用 ``Settings.load``，使当前
                进程重新读取系统环境配置；默认 ``False``，便于只需要持久化的调用方保持明确语义。

        返回:
            写入并（如请求）重载后的脱敏配置快照。

        异常:
            EnvironmentConfigurationError: 字段名、操作或值不符合白名单契约。
            OSError / ValueError: 写入或运行时配置重载失败；配置文件可能已经写入，调用方应记录
                失败并提示用户检查运行时状态。

        副作用:
            在系统 `.env` 中原子写入配置（只重写受管键，其余行原样保留）；``reload_after_write``
            为 ``True`` 时还会更新当前进程的 ``Settings`` 类级配置与由其派生的路径环境。
        """

        unknown = sorted(set(changes) - set(ENVIRONMENT_FIELDS))
        if unknown:
            raise EnvironmentConfigurationError(f"不允许的环境配置字段: {', '.join(unknown)}")
        values: dict[str, str | None] = {}
        for name, change in changes.items():
            field = ENVIRONMENT_FIELDS[name]
            if change.operation == "unchanged":
                continue
            if change.operation == "clear":
                values[name] = None
                continue
            if change.operation != "replace":
                raise EnvironmentConfigurationError(f"不支持的环境配置操作: {change.operation}")
            values[name] = self._validate_value(field, change.value)
        with self.store.locked(self.env_file):
            current = (
                self.store.read_text(self.env_file, root=self.root)
                if self.env_file.exists()
                else ""
            )
            next_content = self._apply_values(current, values)
            self.store.write_text_atomic(self.env_file, next_content, root=self.root)
        if reload_after_write:
            self.reload_runtime_settings()
        log.info(
            "configuration_environment_written",
            extra={"msg": "系统环境配置已保存", "data": {"field_count": len(values)}},
        )
        return self.read()

    @staticmethod
    def reload_runtime_settings() -> None:
        """重新加载当前后端进程的系统环境配置，并重装配受其影响的进程级组件。

        先经运行时配置的唯一加载入口 ``Settings.load`` 刷新 ``Settings`` 与进程环境，再用新值
        替换 Registry 中的主 Agent profile。主 Agent 的 ``max_steps`` 属于 profile 配置，不能
        只更新 ``Settings``；否则 Registry 仍会继续向后续 Run 提供旧值。

        只替换共享 Registry 中的 profile，不改动已经派生出 per-run profile 或 workflow state 的
        执行过程，因此新 ``max_steps`` 只对下一轮新建 Run 生效。

        返回:
            无。

        异常:
            OSError / ValueError: 配置文件读取失败或配置值无法解析，交由 API 层映射为配置错误。
            RuntimeError: Agent Registry 尚未装配时抛出（配置早于应用启动完成时才会出现）。

        副作用:
            重新读取系统 `.env`，更新 ``Settings`` 类级字段与派生路径，并替换进程级主 Agent
            profile；不改动运行中的 Run。
        """

        Settings.load()
        from app.core.observability.langfuse_runtime import reload_langfuse_from_settings

        reload_langfuse_from_settings()
        from app.config.configuration import replace_main_agent_profile

        replace_main_agent_profile(max_steps=Settings.MAIN_AGENT_MAX_STEPS)

    @staticmethod
    def _validate_value(field: EnvironmentField, value: Any) -> str:
        if field.value_type == "boolean":
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, str) and value.strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
                "0",
                "false",
                "no",
                "off",
            }:
                return value.strip().lower()
            raise EnvironmentConfigurationError(f"{field.name} 必须是布尔值")
        if field.value_type == "integer":
            if isinstance(value, bool) or not isinstance(value, str | int):
                raise EnvironmentConfigurationError(f"{field.name} 必须是整数")
            try:
                parsed = int(str(value).strip())
            except ValueError as exc:
                raise EnvironmentConfigurationError(f"{field.name} 必须是整数") from exc
            if field.minimum is not None and parsed < field.minimum:
                raise EnvironmentConfigurationError(f"{field.name} 不能小于 {field.minimum}")
            if field.maximum is not None and parsed > field.maximum:
                raise EnvironmentConfigurationError(f"{field.name} 不能大于 {field.maximum}")
            return str(parsed)
        if not isinstance(value, str):
            raise EnvironmentConfigurationError(f"{field.name} 必须是字符串")
        if field.component == "select" and value not in {option for option, _ in field.options}:
            raise EnvironmentConfigurationError(f"{field.name} 的值不在可选范围内")
        return value

    def _read_values(self, path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        content = self.store.read_text(path, root=self.root)
        return {
            key: value
            for key, value in dotenv_values(stream=StringIO(content)).items()
            if value is not None
        }

    @staticmethod
    def _apply_values(content: str, values: Mapping[str, str | None]) -> str:
        lines = content.splitlines()
        seen: set[str] = set()
        output: list[str] = []
        for line in lines:
            match = _ENV_KEY_RE.match(line)
            if not match or match.group(1) not in values:
                output.append(line)
                continue
            key = match.group(1)
            seen.add(key)
            if values[key] is not None:
                output.append(f"{key}={json.dumps(values[key], ensure_ascii=False)}")
        for key, value in values.items():
            if key not in seen and value is not None:
                if output and output[-1] != "":
                    output.append("")
                output.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
        return "\n".join(output).rstrip("\n") + ("\n" if output else "")
