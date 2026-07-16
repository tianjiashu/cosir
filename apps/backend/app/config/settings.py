"""后端应用的运行时配置。"""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Dict

from dotenv import dotenv_values


@dataclass(frozen=True)
class BackendSettings:
    """存储运行时服务使用的后端配置。

    参数:
        project_root: 用于安全文件访问的项目根目录绝对路径。
        log_dir: 后端日志目录的绝对路径。
        database_file: 后端 SQLite 数据库文件的绝对路径。
        log_database_file: 独立日志 SQLite 数据库文件的绝对路径；省略时从 database_file 派生。
        sqlite_logging_enabled: 是否启用 SQLite 日志落库。
        log_queue_size: SQLite 日志内存队列容量。
        log_batch_size: SQLite 日志批量写入大小。
        log_flush_interval_ms: SQLite 日志最大 flush 间隔毫秒数。
        log_query_limit_max: 日志查询允许的最大 limit。
        model_provider: 模型服务商选择器，当前为 ``echo`` 或 ``openai-compatible``。
        model_base_url: OpenAI 兼容服务商的基础 URL。
        model_api_key_env: 包含服务商 API Key 的环境变量。
        model_name: 发送给服务商的模型名。
        model_thinking_mode: DeepSeek thinking 模式开关，当前为 ``enabled`` 或 ``disabled``。
        max_steps: 一次任务在运行失败前允许的最大运行时步骤数。
        tool_error_limit: 运行失败前允许的最大工具错误数。
        max_context_chars: 模型调用前允许的最大字符数代理预算。

    返回:
        一个不可变的配置值对象。

    异常:
        ValueError: 如果数值限制小于 1，或服务商不受支持。

    副作用:
        无。
    """

    project_root: Path
    log_dir: Path
    database_file: Path
    log_database_file: Path | None = None
    sqlite_logging_enabled: bool = True
    log_queue_size: int = 1000
    log_batch_size: int = 50
    log_flush_interval_ms: int = 1000
    log_query_limit_max: int = 1000
    model_provider: str = "echo"
    model_base_url: str = "https://api.deepseek.com/v1"
    model_api_key_env: str = "DEEPSEEK_API_KEY"
    model_name: str = "deepseek-v4-flash"
    model_thinking_mode: str = "disabled"
    max_steps: int = 100
    tool_error_limit: int = 3
    max_context_chars: int = 20000

    def __post_init__(self) -> None:
        """在 dataclass 初始化之后校验配置。

        参数:
            无。

        返回:
            无。

        异常:
            ValueError: 如果数值限制小于 1，或服务商不受支持。

        副作用:
            无。
        """

        if self.max_steps < 1:
            raise ValueError("max_steps must be greater than zero")
        if self.tool_error_limit < 1:
            raise ValueError("tool_error_limit must be greater than zero")
        if self.max_context_chars < 1:
            raise ValueError("max_context_chars must be greater than zero")
        if self.log_database_file is None:
            object.__setattr__(self, "log_database_file", self.database_file.with_name("logs.sqlite3"))
        if self.log_queue_size < 1:
            raise ValueError("log_queue_size must be greater than zero")
        if self.log_batch_size < 1:
            raise ValueError("log_batch_size must be greater than zero")
        if self.log_flush_interval_ms < 1:
            raise ValueError("log_flush_interval_ms must be greater than zero")
        if self.log_query_limit_max < 1:
            raise ValueError("log_query_limit_max must be greater than zero")
        if self.model_provider not in {"echo", "openai-compatible"}:
            raise ValueError("model_provider must be 'echo' or 'openai-compatible'")
        if self.model_thinking_mode not in {"enabled", "disabled"}:
            raise ValueError("model_thinking_mode must be 'enabled' or 'disabled'")

    @property
    def log_file(self) -> Path:
        """返回当前日期日志文件路径。

        参数:
            无。

        返回:
            根据 ``log_dir`` 和当前日期派生出的 ``logs-YYYY-MM-DD.log`` 路径。

        异常:
            无。

        副作用:
            读取系统日期，但不创建目录或文件。
        """

        from app.config.logging import current_log_file

        return current_log_file(self.log_dir)


def _load_local_env(repository_root: Path) -> None:
    """从约定的本地 env 文件加载未显式设置的环境变量。

    参数:
        repository_root: 仓库根目录绝对路径。

    返回:
        无。

    异常:
        OSError: 当 env 文件存在但无法读取时抛出。

    副作用:
        将 `.env` / `.env.local` 中的键值对写入当前进程环境，但不会覆盖已存在的环境变量。
    """

    backend_root = repository_root / "apps" / "backend"
    merged_values: Dict[str, str] = {}
    for env_file in (
        repository_root / ".env",
        backend_root / ".env",
        repository_root / ".env.local",
        backend_root / ".env.local",
    ):
        if not env_file.exists():
            continue
        file_values = dotenv_values(env_file)
        for key, value in file_values.items():
            if value is not None:
                merged_values[key] = value
    for key, value in merged_values.items():
        os.environ.setdefault(key, value)


def _resolve_default_model_provider() -> str:
    """根据当前环境推导模型服务商默认值。

    参数:
        无。

    返回:
        显式设置的模型服务商；若未显式设置且存在 API Key，则返回 ``openai-compatible``，
        否则回退到 ``echo``。

    异常:
        无。

    副作用:
        无。
    """

    explicit_provider = os.environ.get("CODING_AGENT_MODEL_PROVIDER")
    if explicit_provider:
        return explicit_provider
    api_key_env = os.environ.get("CODING_AGENT_MODEL_API_KEY_ENV", "DEEPSEEK_API_KEY")
    if os.environ.get(api_key_env):
        return "openai-compatible"
    return "echo"


def default_settings() -> BackendSettings:
    """为本地开发构建默认后端配置。

    参数:
        无。

    返回:
        使用本地路径与环境变量覆盖的 BackendSettings。

    异常:
        RuntimeError: 如果无法从本文件路径推导出仓库根目录。

    副作用:
        无。
    """

    repository_root = Path(__file__).resolve().parents[4]
    _load_local_env(repository_root)
    return BackendSettings(
        project_root=repository_root,
        log_dir=repository_root / "logs",
        database_file=repository_root / "storage" / "app.sqlite3",
        log_database_file=Path(
            os.environ.get(
                "CODING_AGENT_LOG_DATABASE_FILE",
                str(repository_root / "storage" / "logs.sqlite3"),
            )
        ),
        sqlite_logging_enabled=_env_bool("CODING_AGENT_SQLITE_LOGGING_ENABLED", True),
        log_queue_size=int(os.environ.get("CODING_AGENT_LOG_QUEUE_SIZE", "1000")),
        log_batch_size=int(os.environ.get("CODING_AGENT_LOG_BATCH_SIZE", "50")),
        log_flush_interval_ms=int(os.environ.get("CODING_AGENT_LOG_FLUSH_INTERVAL_MS", "1000")),
        log_query_limit_max=int(os.environ.get("CODING_AGENT_LOG_QUERY_LIMIT_MAX", "1000")),
        model_provider=_resolve_default_model_provider(),
        model_base_url=os.environ.get(
            "CODING_AGENT_MODEL_BASE_URL",
            "https://api.deepseek.com",
        ),
        model_api_key_env=os.environ.get(
            "CODING_AGENT_MODEL_API_KEY_ENV",
            "DEEPSEEK_API_KEY",
        ),
        model_name=os.environ.get("CODING_AGENT_MODEL_NAME", "deepseek-v4-flash"),
        model_thinking_mode=os.environ.get("CODING_AGENT_MODEL_THINKING_MODE", "disabled"),
        max_steps=int(os.environ.get("CODING_AGENT_MAX_STEPS", "8")),
        tool_error_limit=int(os.environ.get("CODING_AGENT_TOOL_ERROR_LIMIT", "3")),
        max_context_chars=int(os.environ.get("CODING_AGENT_MAX_CONTEXT_CHARS", "20000")),
    )


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量。

    参数:
        name: 环境变量名。
        default: 未设置时使用的默认值。

    返回:
        解析后的布尔值。

    异常:
        ValueError: 如果变量值不是受支持的布尔文本。

    副作用:
        读取进程环境变量。
    """

    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")
