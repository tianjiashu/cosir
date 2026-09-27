"""系统级 env 配置 service。

只允许编辑 Settings 已声明的白名单字段。读取结果同时表达磁盘值、进程有效值和来源，Secret
只返回配置状态，不返回原文。UI 写入统一落到 `.env.local`，基础 `.env` 保留为用户维护文件。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values

from app.config.logging.logger import log
from app.config.settings import Settings
from app.service.configuration.file_store import ConfigurationFileStore
from app.utils.cosir_paths import system_cosir_dir, system_env_file, system_env_local_file


class EnvironmentConfigurationError(ValueError):
    """env 配置字段、类型或操作不符合白名单契约。"""


@dataclass(frozen=True)
class EnvironmentField:
    name: str
    value_type: str
    secret: bool
    default: str | bool | None


@dataclass(frozen=True)
class EnvironmentChange:
    operation: Literal["replace", "clear", "unchanged"]
    value: Any = None


_FIELDS: dict[str, EnvironmentField] = {
    "DEFAULT_LANGUAGE": EnvironmentField("DEFAULT_LANGUAGE", "string", False, "zh"),
    "WEB_BACKEND": EnvironmentField("WEB_BACKEND", "string", False, ""),
    "WEB_SEARCH_BACKEND": EnvironmentField("WEB_SEARCH_BACKEND", "string", False, ""),
    "WEB_EXTRACT_BACKEND": EnvironmentField("WEB_EXTRACT_BACKEND", "string", False, ""),
    "LANGFUSE_ENABLED": EnvironmentField("LANGFUSE_ENABLED", "boolean", False, False),
    "LANGFUSE_PUBLIC_KEY": EnvironmentField("LANGFUSE_PUBLIC_KEY", "string", True, None),
    "LANGFUSE_SECRET_KEY": EnvironmentField("LANGFUSE_SECRET_KEY", "string", True, None),
    "LANGFUSE_BASE_URL": EnvironmentField(
        "LANGFUSE_BASE_URL", "string", False, "http://124.220.55.187"
    ),
}
_ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


class EnvironmentConfigurationService:
    """读取和更新 `.env.local` 白名单覆盖，并按调用方要求重载运行时配置。"""

    def __init__(
        self,
        *,
        env_path: Path | None = None,
        local_path: Path | None = None,
    ) -> None:
        self.env_path = env_path or system_env_file()
        self.local_path = local_path or system_env_local_file()
        self.root = system_cosir_dir()
        self.store = ConfigurationFileStore()

    def read(self) -> list[dict[str, Any]]:
        """返回白名单字段的脱敏来源快照。"""

        self.store.assert_safe_child(self.root, self.env_path)
        self.store.assert_safe_child(self.root, self.local_path)
        base = self._read_values(self.env_path)
        local = self._read_values(self.local_path)
        result: list[dict[str, Any]] = []
        for field in _FIELDS.values():
            process_value = Settings.__dict__.get(field.name, field.default)
            process_override = (
                os.environ.get(field.name) is not None
                and not Settings.is_file_loaded_value(field.name)
            )
            if process_override:
                source = "process"
            elif field.name in local:
                source = "env_local"
            elif field.name in base:
                source = "env"
            else:
                source = "default"
            disk_value = local.get(field.name, base.get(field.name, field.default))
            restart_value = local.get(field.name, base.get(field.name, field.default))
            result.append(
                {
                    "name": field.name,
                    "type": field.value_type,
                    "secret": field.secret,
                    "default": None if field.secret else field.default,
                    "value": None if field.secret else process_value,
                    "disk_value": None if field.secret else disk_value,
                    "restart_value": None if field.secret else restart_value,
                    "configured": process_value is not None and str(process_value) != "",
                    "masked": field.secret and disk_value is not None and str(disk_value) != "",
                    "source": source,
                    "restart_required": False,
                    "process_override": process_override,
                }
            )
        return result

    def update(
        self,
        changes: Mapping[str, EnvironmentChange],
        *,
        reload_after_write: bool = False,
    ) -> list[dict[str, Any]]:
        """校验并更新 `.env.local`，可选地重载当前进程配置后返回脱敏快照。

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
            在系统 `.env.local` 中原子写入配置；``reload_after_write`` 为 ``True`` 时还会更新
            当前进程的 ``Settings`` 类级配置与由其派生的路径环境。
        """

        unknown = sorted(set(changes) - set(_FIELDS))
        if unknown:
            raise EnvironmentConfigurationError(f"不允许的环境配置字段: {', '.join(unknown)}")
        values: dict[str, str | None] = {}
        for name, change in changes.items():
            field = _FIELDS[name]
            if change.operation == "unchanged":
                continue
            if change.operation == "clear":
                values[name] = None
                continue
            if change.operation != "replace":
                raise EnvironmentConfigurationError(f"不支持的环境配置操作: {change.operation}")
            values[name] = self._validate_value(field, change.value)
        self.store.assert_safe_child(self.root, self.local_path)
        with self.store.locked(self.local_path):
            current = (
                self.store.read_text(self.local_path, root=self.root)
                if self.local_path.exists()
                else ""
            )
            next_content = self._apply_values(current, values)
            self.store.write_text_atomic(self.local_path, next_content, root=self.root)
        if reload_after_write:
            self.reload_runtime_settings()
        log.info(
            "configuration_environment_written",
            extra={"msg": "系统环境配置已保存", "data": {"field_count": len(values)}},
        )
        return self.read()

    @staticmethod
    def reload_runtime_settings() -> None:
        """重新加载当前后端进程的系统环境配置。

        该方法只负责调用运行时配置的唯一加载入口，不重建 Agent、工具系统或其他生命周期资源；
        这些组件读取的 Web provider、语言和观测配置会在后续调用中使用最新的 ``Settings`` 值。

        返回:
            无。

        异常:
            OSError / ValueError: 配置文件读取失败或配置值无法解析，交由 API 层映射为配置错误。

        副作用:
            重新读取系统 `.env` / `.env.local`，更新 ``Settings`` 类级字段，并同步路径配置。
        """

        Settings.load()

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
        if not isinstance(value, str):
            raise EnvironmentConfigurationError(f"{field.name} 必须是字符串")
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
