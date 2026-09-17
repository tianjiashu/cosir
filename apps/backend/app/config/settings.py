"""后端应用的运行时配置（类级静态命名空间）。

这些运行期配置作为 ``Settings`` 类的类级静态属性存在，由 ``Settings.load`` 在进程启动时
填充一次，之后所有模块通过 ``from app.config.settings import Settings`` 后静态读取
（如 ``Settings.LOG_DIR``），配置对象不再被到处传递。

设计边界：
- 本模块只承载进程级运行配置（日志路径、SQLite 路径、各类数值上限）。模型相关配置不在
  此处，统一收敛到 ``app.core.llm.model_settings``。
- 数值上限类配置（如 ``Settings.TOOL_ERROR_LIMIT``）为全进程共享的静态值，运行时不确、
  不可变；需要按环境覆盖时经环境变量或 ``Settings.override``（测试）注入。单轮最大步数
  ``max_steps`` 不再在此定义，唯一来源为 ``AgentProfile.max_steps``（编排层经
  ``workflow.py`` 初始化 input_state 注入）。
- 路径类配置（``Settings.LOG_DIR`` / ``Settings.DATABASE_FILE`` 等）由仓库根目录推导，受
  ``.env`` 覆盖；测试可将临时目录经 ``Settings.override`` 注入以隔离副作用。
"""

import os
from pathlib import Path
from typing import Any, ClassVar

from dotenv import dotenv_values


class Settings:
    """后端运行时配置（类级静态属性，进程级单例命名空间）。

    配置作为类级静态属性存在，由 ``Settings.load`` 在进程启动时填充一次，之后所有模块通过
    ``Settings.LOG_DIR`` 等静态读取，不实例化、不传递 ``Settings`` 对象。

    职责边界：
        - 负责：进程级运行配置的定义、加载（含 ``.env`` 覆盖）、校验与按测试注入。
        - 不负责：模型相关配置（见 ``app.core.llm.model_settings``）、任何业务读写。
    """

    # --- 类级静态配置（进程启动后由 ``Settings.load`` 填充，之后只读） ---
    LOG_DIR: ClassVar[Path] = Path("logs")
    DATABASE_FILE: ClassVar[Path] = Path("storage/app.sqlite3")
    LOG_DATABASE_FILE: ClassVar[Path | None] = None
    CHECKPOINT_FILE: ClassVar[Path | None] = None
    SQLITE_LOGGING_ENABLED: ClassVar[bool] = True
    LOG_QUEUE_SIZE: ClassVar[int] = 1000
    LOG_BATCH_SIZE: ClassVar[int] = 50
    LOG_FLUSH_INTERVAL_MS: ClassVar[int] = 1000
    LOG_QUERY_LIMIT_MAX: ClassVar[int] = 1000
    LOG_MAX_BYTES: ClassVar[int] = 5 * 1024 * 1024
    LOG_BACKUP_COUNT: ClassVar[int] = 7
    TOOL_ERROR_LIMIT: ClassVar[int] = 10
    MAX_PARALLEL_TOOL_CALLS: ClassVar[int] = 8
    # 工具结果摘要中 content 的截断上限（字符），供 observe 节点与阶段二 LLM 观察使用，
    # 避免把大体积工具输出塞进 checkpoint。
    TOOL_OBSERVATION_CONTEXT_LIMIT: ClassVar[int] = 4000
    MAX_CONTEXT_CHARS: ClassVar[int] = 20000
    MAX_TOOL_OUTPUT_CHARS: ClassVar[int] = 20000

    # 面向用户的默认回复语言（如 zh / en）：作为全局配置，统一驱动系统提示词与运行时
    # 上下文；需要本地化覆盖时经环境变量 ``CODING_AGENT_DEFAULT_LANGUAGE`` 注入。
    DEFAULT_LANGUAGE: ClassVar[str] = "zh"

    # 上下文窗口软上限（token）：上下文占用圆环 100% 基准的上限之一，与「模型最大窗口」
    # 取 min 后作为实际上限（分母）。0 表示不设软上限，只用模型自身最大窗口。可由
    # CODING_AGENT_CONTEXT_WINDOW_TOKENS 经环境变量覆盖（如 32000 以省成本/控延迟）。
    CONTEXT_WINDOW_TOKENS: ClassVar[int] = 200000
    WEB_SEARCH_BACKEND: ClassVar[str] = ""
    WEB_EXTRACT_BACKEND: ClassVar[str] = ""
    WEB_BACKEND: ClassVar[str] = ""
    WEB_REQUEST_TIMEOUT_SECONDS: ClassVar[float] = 20.0
    WEB_SEARCH_LIMIT_MAX: ClassVar[int] = 20
    WEB_EXTRACT_URL_LIMIT_MAX: ClassVar[int] = 5
    WEB_EXTRACT_CHAR_LIMIT: ClassVar[int] = 15000

    # 模型流式 chunk 调试落盘开关：默认关闭。开启后 ``debug_dump`` 会逐 chunk / 合并后
    # 把完整消息 JSON 追加到 ``logs/debug_*_chunks.jsonl``，用于本地排查 chunk 结构。
    # 该通道绕过常规日志预算截断，且每 turn 写盘量较大，常驻生产会损害稳定迭代，故默认关闭，
    # 仅在需要排查流式 chunk 结构时经环境变量 ``CODING_AGENT_DEBUG_DUMP_CHUNKS=true`` 显式开启。
    DEBUG_DUMP_CHUNKS: ClassVar[bool] = False
    # 模型生成种子：调试阶段用于让模型输出可复现（相同输入 + 相同 seed 尽量得到一致结果）。
    # 经 ``CODING_AGENT_LLM_SEED`` 覆盖；空串/未设置时取 None（不固定种子，由 API 随机）。
    # 生产环境应保持 None，避免每次回答高度一致导致体验僵化。
    LLM_SEED: ClassVar[int | None] = None

    # --- CodeGraph 索引生命周期（见 workspace_payload-workspace-lifecycle-design.md） ---
    # 首次建索引（init）大仓库可能数分钟，需长超时；增量同步（sync）耗时较短。
    CODEGRAPH_INDEX_INIT_TIMEOUT_SECONDS: ClassVar[float] = 600.0
    CODEGRAPH_INDEX_SYNC_TIMEOUT_SECONDS: ClassVar[float] = 120.0

    # CodeGraph 总开关：True 启用（启动 Kernel 常驻 + 注册 6 个查询工具 + 注册索引保活
    # Hook + agent 白名单含 codegraph + 系统提示词含 codegraph 指引）；False 关闭（完全不
    # 挂载 Kernel、不注册工具/Hook，模型侧无任何 codegraph 入口）。默认关闭（False），
    # 经 CODING_AGENT_CODEGRAPH_ENABLED 环境变量覆盖（true/false）。
    CODEGRAPH_ENABLED: ClassVar[bool] = False

    # --- 系统提示词三层构建（动态变量 / Agent 预设 / Workspace 项目指令） ---
    # Layer 2：Agent 系统预设文件（AgentProfile.prompt_file_path）加载上限，避免超大预设
    # 撑爆上下文；经 ``CODING_AGENT_AGENT_PERSONA_*`` 覆盖。
    AGENT_PERSONA_MAX_BYTES: ClassVar[int] = 100_000
    AGENT_PERSONA_MAX_TOKENS: ClassVar[int] = 2_000
    # Layer 1：动态变量层字节硬上限（内容小且固定，仅防御性截断）；经
    # ``CODING_AGENT_RUNTIME_CONTEXT_MAX_BYTES`` 覆盖。
    RUNTIME_CONTEXT_MAX_BYTES: ClassVar[int] = 4_000

    # Layer 3：Workspace 项目指令预算闸门；经 ``CODING_AGENT_WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS``
    # 覆盖。候选文件名唯一（``AGENTS.md``，硬编码在 ``system_prompt_builder`` 内、不经配置注入），
    # 最终只加载唯一一个指令文件，因此不存在文件数闸门；单文件注入上下文的 token 上限由
    # ``WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS`` 约束，字节上限为 ``system_prompt_builder`` 模块内
    # 固定安全兜底（非配置项，先于 token 估算做廉价截断，防止超大文件撑爆上下文）。
    WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS: ClassVar[int] = 1_200

    # --- 委派子Agent并发执行（见 docs/委派子Agent并发执行技术方案.md §6.1） ---
    # 并发上限：单进程内同时运行的 child 委派数上限（第一版决策定为 2）；软超时：
    # child 委派单次执行的生效超时（async 路径），不等同于线程硬杀，与工具定义
    # ``timeout_seconds`` 元数据不双轨生效。
    DELEGATION_MAX_CONCURRENCY: ClassVar[int] = 4
    DELEGATION_TIMEOUT_SECONDS: ClassVar[float] = 300.0

    # --- LLM 请求全局默认值（所有模型统一，除非 Agent 级 ModelSettings 显式覆盖） ---
    # 请求超时：单次 ChatOpenAI HTTP 请求超时（秒），覆盖默认 600s 以更快失败重试；
    # 经 ``CODING_AGENT_LLM_REQUEST_TIMEOUT_SECONDS`` 覆盖。
    LLM_REQUEST_TIMEOUT_SECONDS: ClassVar[float] = 120.0
    # 请求重试次数：SDK 层失败重试上限（不含超时本身的首次尝试）；经
    # ``CODING_AGENT_LLM_MAX_RETRIES`` 覆盖。0 表示不重试。
    LLM_MAX_RETRIES: ClassVar[int] = 2

    # --- Langfuse 可观测性（云服务器自托管，详见 docs/Langfuse可观测性集成技术方案.md） ---
    # 启用开关 + 密钥齐备 + langfuse 可导入，三者满足 ``tracing_enabled()`` 才返回 True。
    # 密钥仅通过环境变量（``CODING_AGENT_LANGFUSE_*``）注入，不写入代码库，避免泄露。
    LANGFUSE_ENABLED: ClassVar[bool] = False
    LANGFUSE_PUBLIC_KEY: ClassVar[str | None] = None
    LANGFUSE_SECRET_KEY: ClassVar[str | None] = None
    # 云服务器经反向代理对外暴露的 HTTPS 域名（指向 langfuse/server）。
    LANGFUSE_BASE_URL: ClassVar[str] = "http://124.220.55.187"

    # 允许被 ``override`` 覆盖的字段名集合；实际值在 ``Settings`` 类定义结束后由
    # ``_finalize_overridable`` 经 ``Settings.__annotations__`` 推导注入，规避类体内裸
    # ``__annotations__`` 的 IDE 静态解析告警；占位为空集，置位见 ``_finalize_overridable``。
    _OVERRIDABLE: ClassVar[frozenset[str]] = frozenset()

    @staticmethod
    def repository_root() -> Path:
        """推导仓库根目录绝对路径（公开契约，供跨模块安全调用）。

        参数:
            无。

        返回:
            仓库根目录绝对路径（本文件位于 ``<repo>/apps/backend/app/config/settings.py``，
            上溯四级即仓库根）。

        异常:
            无。

        副作用:
            无。
        """

        return Path(__file__).resolve().parents[4]

    @staticmethod
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
        merged_values: dict[str, str] = {}
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

    @staticmethod
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

    @classmethod
    def _validate(cls) -> None:
        """校验数值类配置是否合法。

        参数:
            无。

        返回:
            无。

        异常:
            ValueError: 如果任一数值上限小于 1，或任一超时秒数不大于 0。

        副作用:
            无。
        """

        if cls.TOOL_ERROR_LIMIT < 1:
            raise ValueError("TOOL_ERROR_LIMIT must be greater than zero")
        if cls.MAX_PARALLEL_TOOL_CALLS < 1:
            raise ValueError("MAX_PARALLEL_TOOL_CALLS must be greater than zero")
        if cls.DELEGATION_MAX_CONCURRENCY < 1:
            raise ValueError("DELEGATION_MAX_CONCURRENCY must be greater than zero")
        if cls.DELEGATION_TIMEOUT_SECONDS <= 0:
            raise ValueError("DELEGATION_TIMEOUT_SECONDS must be greater than zero")
        if cls.LLM_REQUEST_TIMEOUT_SECONDS <= 0:
            raise ValueError("LLM_REQUEST_TIMEOUT_SECONDS must be greater than zero")
        if cls.LLM_MAX_RETRIES < 0:
            raise ValueError("LLM_MAX_RETRIES must be greater than or equal to zero")
        if cls.LLM_SEED is not None and cls.LLM_SEED < 0:
            raise ValueError("LLM_SEED must be greater than or equal to zero")
        if cls.MAX_CONTEXT_CHARS < 1:
            raise ValueError("MAX_CONTEXT_CHARS must be greater than zero")
        if cls.MAX_TOOL_OUTPUT_CHARS < 1:
            raise ValueError("MAX_TOOL_OUTPUT_CHARS must be greater than zero")
        if cls.CONTEXT_WINDOW_TOKENS < 0:
            raise ValueError("CONTEXT_WINDOW_TOKENS must not be negative")
        if cls.LOG_QUEUE_SIZE < 1:
            raise ValueError("LOG_QUEUE_SIZE must be greater than zero")
        if cls.LOG_BATCH_SIZE < 1:
            raise ValueError("LOG_BATCH_SIZE must be greater than zero")
        if cls.LOG_FLUSH_INTERVAL_MS < 1:
            raise ValueError("LOG_FLUSH_INTERVAL_MS must be greater than zero")
        if cls.LOG_QUERY_LIMIT_MAX < 1:
            raise ValueError("LOG_QUERY_LIMIT_MAX must be greater than zero")
        if cls.LOG_MAX_BYTES < 1:
            raise ValueError("LOG_MAX_BYTES must be greater than zero")
        if cls.LOG_BACKUP_COUNT < 1:
            raise ValueError("LOG_BACKUP_COUNT must be greater than zero")
        if cls.WEB_REQUEST_TIMEOUT_SECONDS <= 0:
            raise ValueError("WEB_REQUEST_TIMEOUT_SECONDS must be greater than zero")
        if cls.WEB_SEARCH_LIMIT_MAX < 1:
            raise ValueError("WEB_SEARCH_LIMIT_MAX must be greater than zero")
        if cls.WEB_EXTRACT_URL_LIMIT_MAX < 1:
            raise ValueError("WEB_EXTRACT_URL_LIMIT_MAX must be greater than zero")
        if cls.WEB_EXTRACT_CHAR_LIMIT < 1:
            raise ValueError("WEB_EXTRACT_CHAR_LIMIT must be greater than zero")
        if cls.TOOL_OBSERVATION_CONTEXT_LIMIT < 1:
            raise ValueError("TOOL_OBSERVATION_CONTEXT_LIMIT must be greater than zero")
        if cls.AGENT_PERSONA_MAX_BYTES < 1:
            raise ValueError("AGENT_PERSONA_MAX_BYTES must be greater than zero")
        if cls.AGENT_PERSONA_MAX_TOKENS < 1:
            raise ValueError("AGENT_PERSONA_MAX_TOKENS must be greater than zero")
        if cls.RUNTIME_CONTEXT_MAX_BYTES < 1:
            raise ValueError("RUNTIME_CONTEXT_MAX_BYTES must be greater than zero")
        if cls.WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS < 1:
            raise ValueError("WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS must be greater than zero")

    @classmethod
    def load(cls, repository_root: Path | None = None) -> None:
        """加载默认配置与本地 env 覆盖，填充类级静态属性。

        进程启动时调用一次（``__main__.py`` 与 ``app.py`` 的 lifespan 均会调用）；模块导入时
        亦会调用一次，使未显式启动的单元测试也能拿到仓库根推导出的默认路径。

        参数:
            repository_root: 仓库根目录绝对路径；省略时从本文件路径推导。

        返回:
            无。

        异常:
            ValueError: 如果数值配置非法（数值上限小于 1，或超时秒数不大于 0）。

        副作用:
            加载 ``.env`` / ``.env.local`` 到进程环境；覆盖本类全部静态属性。
        """

        root = repository_root or cls.repository_root()
        cls._load_local_env(root)

        cls.LOG_DIR = Path(os.environ.get("CODING_AGENT_LOG_DIR", str(root / "logs")))
        cls.DATABASE_FILE = root / "storage" / "app.sqlite3"
        cls.LOG_DATABASE_FILE = Path(
            os.environ.get(
                "CODING_AGENT_LOG_DATABASE_FILE",
                str(root / "storage" / "logs.sqlite3"),
            )
        )
        cls.CHECKPOINT_FILE = Path(
            os.environ.get(
                "CODING_AGENT_CHECKPOINT_FILE",
                str(root / "storage" / "langgraph_checkpoints.sqlite"),
            )
        )
        cls.SQLITE_LOGGING_ENABLED = cls._env_bool("CODING_AGENT_SQLITE_LOGGING_ENABLED", True)
        cls.LOG_QUEUE_SIZE = int(os.environ.get("CODING_AGENT_LOG_QUEUE_SIZE", "1000"))
        cls.LOG_BATCH_SIZE = int(os.environ.get("CODING_AGENT_LOG_BATCH_SIZE", "50"))
        cls.LOG_FLUSH_INTERVAL_MS = int(
            os.environ.get("CODING_AGENT_LOG_FLUSH_INTERVAL_MS", "1000")
        )
        cls.LOG_QUERY_LIMIT_MAX = int(os.environ.get("CODING_AGENT_LOG_QUERY_LIMIT_MAX", "1000"))
        cls.LOG_MAX_BYTES = int(os.environ.get("CODING_AGENT_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
        cls.LOG_BACKUP_COUNT = int(os.environ.get("CODING_AGENT_LOG_BACKUP_COUNT", "7"))
        cls.TOOL_ERROR_LIMIT = int(os.environ.get("CODING_AGENT_TOOL_ERROR_LIMIT", "3"))
        cls.MAX_PARALLEL_TOOL_CALLS = int(
            os.environ.get("CODING_AGENT_MAX_PARALLEL_TOOL_CALLS", "8")
        )
        cls.DELEGATION_MAX_CONCURRENCY = int(
            os.environ.get("CODING_AGENT_DELEGATION_MAX_CONCURRENCY", "2")
        )
        cls.DELEGATION_TIMEOUT_SECONDS = float(
            os.environ.get("CODING_AGENT_DELEGATION_TIMEOUT_SECONDS", "300")
        )
        cls.LLM_REQUEST_TIMEOUT_SECONDS = float(
            os.environ.get("CODING_AGENT_LLM_REQUEST_TIMEOUT_SECONDS", "120")
        )
        cls.LLM_MAX_RETRIES = int(os.environ.get("CODING_AGENT_LLM_MAX_RETRIES", "2"))
        _raw_seed = os.environ.get("CODING_AGENT_LLM_SEED", "")
        cls.LLM_SEED = int(_raw_seed) if _raw_seed else None
        cls.TOOL_OBSERVATION_CONTEXT_LIMIT = int(
            os.environ.get("CODING_AGENT_TOOL_OBSERVATION_CONTEXT_LIMIT", "4000")
        )
        cls.MAX_CONTEXT_CHARS = int(os.environ.get("CODING_AGENT_MAX_CONTEXT_CHARS", "20000"))
        cls.MAX_TOOL_OUTPUT_CHARS = int(
            os.environ.get("CODING_AGENT_MAX_TOOL_OUTPUT_CHARS", "20000")
        )
        cls.DEFAULT_LANGUAGE = os.environ.get("CODING_AGENT_DEFAULT_LANGUAGE", "zh").strip().lower()
        cls.CONTEXT_WINDOW_TOKENS = int(
            os.environ.get("CODING_AGENT_CONTEXT_WINDOW_TOKENS", "200000")
        )
        cls.WEB_SEARCH_BACKEND = (
            os.environ.get("CODING_AGENT_WEB_SEARCH_BACKEND", "").strip().lower()
        )
        cls.WEB_EXTRACT_BACKEND = (
            os.environ.get("CODING_AGENT_WEB_EXTRACT_BACKEND", "").strip().lower()
        )
        cls.WEB_BACKEND = os.environ.get("CODING_AGENT_WEB_BACKEND", "").strip().lower()
        cls.WEB_REQUEST_TIMEOUT_SECONDS = float(
            os.environ.get("CODING_AGENT_WEB_REQUEST_TIMEOUT_SECONDS", "20")
        )
        cls.WEB_SEARCH_LIMIT_MAX = int(os.environ.get("CODING_AGENT_WEB_SEARCH_LIMIT_MAX", "20"))
        cls.WEB_EXTRACT_URL_LIMIT_MAX = int(
            os.environ.get("CODING_AGENT_WEB_EXTRACT_URL_LIMIT_MAX", "5")
        )
        cls.WEB_EXTRACT_CHAR_LIMIT = int(
            os.environ.get("CODING_AGENT_WEB_EXTRACT_CHAR_LIMIT", "15000")
        )
        cls.DEBUG_DUMP_CHUNKS = cls._env_bool("CODING_AGENT_DEBUG_DUMP_CHUNKS", False)

        # CodeGraph 总开关（默认关闭；显式开启才挂载 Kernel 与注册 codegraph 工具）。
        cls.CODEGRAPH_ENABLED = cls._env_bool("CODING_AGENT_CODEGRAPH_ENABLED", False)

        # 系统提示词三层构建配置（动态变量 / Agent 预设 / Workspace 项目指令）。
        cls.AGENT_PERSONA_MAX_BYTES = int(
            os.environ.get("CODING_AGENT_AGENT_PERSONA_MAX_BYTES", "100000")
        )
        cls.AGENT_PERSONA_MAX_TOKENS = int(
            os.environ.get("CODING_AGENT_AGENT_PERSONA_MAX_TOKENS", "2000")
        )
        cls.RUNTIME_CONTEXT_MAX_BYTES = int(
            os.environ.get("CODING_AGENT_RUNTIME_CONTEXT_MAX_BYTES", "4000")
        )
        cls.WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS = int(
            os.environ.get("CODING_AGENT_WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS", "1200")
        )

        # Langfuse 可观测性配置（缺省关闭，显式开启且仅在密钥齐备时生效）。
        cls.LANGFUSE_ENABLED = cls._env_bool("CODING_AGENT_LANGFUSE_ENABLED", False)
        cls.LANGFUSE_PUBLIC_KEY = os.environ.get("CODING_AGENT_LANGFUSE_PUBLIC_KEY")
        cls.LANGFUSE_SECRET_KEY = os.environ.get("CODING_AGENT_LANGFUSE_SECRET_KEY")
        cls.LANGFUSE_BASE_URL = os.environ.get(
            "CODING_AGENT_LANGFUSE_BASE_URL",
            "http://124.220.55.187",
        )

        cls._validate()

    @classmethod
    def override(cls, **kwargs: Any) -> None:
        """覆盖个别类级静态属性，用于测试或特殊场景注入临时配置。

        参数:
            kwargs: 待覆盖的类级静态属性名与值（键必须是本类已定义的静态属性名）。

        返回:
            无。

        异常:
            ValueError: 如果传入了本类不存在的属性名。

        副作用:
            修改本类的全局静态属性；该修改跨测试持续，调用方应自行保证隔离（必要时用
            ``Settings.load()`` 复位）。
        """

        invalid = set(kwargs) - cls._OVERRIDABLE
        if invalid:
            raise ValueError(f"unknown settings to override: {sorted(invalid)}")
        for name, value in kwargs.items():
            setattr(cls, name, value)

    @classmethod
    def _finalize_overridable(cls) -> None:
        """类定义结束后推导 ``_OVERRIDABLE``，固化允许被 ``override`` 覆盖的字段名集合。

        在类体执行完毕后调用，经 ``cls.__annotations__``（属性访问，规避类体内裸
        ``__annotations__`` 的 IDE 静态解析告警）取得全部类级静态属性注解，剔除
        ``_OVERRIDABLE`` 自身后固化为不可变集合，使可覆盖字段与类注解单一事实来源一致，
        不手抄、不漂移。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            将派生结果写入 ``cls._OVERRIDABLE``（类级静态属性）。
        """

        cls._OVERRIDABLE = frozenset(cls.__annotations__) - {"_OVERRIDABLE"}

    @classmethod
    def log_file(cls) -> Path:
        """返回当前日期日志文件路径。

        参数:
            无。

            返回:
                ``Settings.LOG_DIR / backend-YYYY-MM-DD.log``；同日大小分片使用
                ``backend-YYYY-MM-DD.1.log`` 等后缀。

        异常:
            无。

        副作用:
            读取系统日期，但不创建目录或文件。
        """

        # 延迟导入以避免模块级循环依赖：``settings`` 顶层若导入 ``logging.common``，
        # 会触发 ``logging`` 包 ``__init__`` 经 ``configuration -> sqlite_handler ->
        # store_engines -> settings`` 回引自身。改为函数内导入后，``settings`` 模块
        # 顶层零 app 依赖，无论谁先 import 都能立即完成，循环被根治。
        from app.config.logging.common import current_log_file

        return current_log_file(cls.LOG_DIR)


# 类定义结束后推导可覆盖字段集合，再按仓库根推导默认配置，使未显式调用 ``Settings.load``
# 的单元测试也能拿到合法绝对路径；生产启动时再次调用为幂等覆盖（含 env 覆盖与环境差异）。
Settings._finalize_overridable()
Settings.load()
