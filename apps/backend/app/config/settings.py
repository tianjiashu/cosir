"""后端应用的运行时配置（类级静态命名空间）。

这些运行期配置作为 ``Settings`` 类的类级静态属性存在，由 ``Settings.load`` 在进程启动时
填充一次，之后所有模块通过 ``from app.config.settings import Settings`` 后静态读取
（如 ``Settings.WEB_BACKEND``），配置对象不再被到处传递。

设计边界：
- 本模块只承载**运行期可由环境变量 / ``.cosir/.env`` 覆盖**的进程级配置（默认回复语言、
  Web provider 选择、可观测性集成参数），以及需要保密的集成凭据。模型相关配置不在此处，
  统一收敛到 ``app.core.agents.model_settings``。
- **不可变、无需按环境覆盖的数值上限不在此处**：系统提示词预算、工具输出与并发上限、
  Web 限额、日志轮转等固定值统一收敛到 ``app.config.constant.Constant`` 的对应域；判定标准
  是「是否存在真实的按环境覆盖需求」，不是数值大小。
- 进程固定路径（数据根 / 日志目录 / 主库与 checkpoint 文件）不在此定义，唯一事实源为
  ``app.utils.paths``；``Settings.load`` 会触发其 ``reset`` 与环境变量对齐。
- 保留下来的配置全进程共享、启动后只读；测试注入经 ``Settings.override``。单轮最大步数
  ``max_steps`` 不在此定义，唯一来源为 ``AgentProfile.max_steps``（编排层经
  ``workflow.py`` 初始化 input_state 注入）。
"""

import os
from pathlib import Path
from typing import Any, ClassVar

from dotenv import dotenv_values

from app.utils import paths


class Settings:
    """后端运行时配置（类级静态属性，进程级单例命名空间）。

    配置作为类级静态属性存在，由 ``Settings.load`` 在进程启动时填充一次，之后所有模块通过
    ``Settings.WEB_BACKEND`` 等静态读取，不实例化、不传递 ``Settings`` 对象。

    职责边界：
        - 负责：进程级运行配置的定义、加载（含 ``.env`` 覆盖）、校验与按测试注入。
        - 不负责：不可变静态常量（见 ``app.config.constant.Constant``）、模型相关配置
          （见 ``app.core.agents.model_settings``）、进程固定路径（见 ``app.utils.paths``）、
          任何业务读写。
    """

    # --- 类级静态配置（进程启动后由 ``Settings.load`` 填充，之后只读） ---
    # 职责边界提醒：**不可变、且无需按环境覆盖的固定值不在这里**——数值上限、超时、计数、
    # 预算等已收敛到 ``app.config.constant.Constant`` 的对应域（LLM 请求参数、系统提示词预算、
    # 工具输出/并发上限、Web 超时与限额、日志轮转参数）。本类只保留「运行期可由环境变量 /
    # ``.cosir/.env`` 覆盖」的配置，以及需要保密的集成参数。

    # 面向用户的默认回复语言（如 zh / en）：作为全局配置，统一驱动系统提示词与运行时
    # 上下文；需要本地化覆盖时经环境变量 ``CODING_AGENT_DEFAULT_LANGUAGE`` 注入。
    DEFAULT_LANGUAGE: ClassVar[str] = "zh"

    # Web 工具 provider 选择：``WEB_BACKEND`` 是统一开关，两个 per-tool 变量用于按工具覆盖
    # （空串表示不覆盖）；三者都经 ``CODING_AGENT_WEB_*_BACKEND`` 覆盖。
    WEB_SEARCH_BACKEND: ClassVar[str] = ""
    WEB_EXTRACT_BACKEND: ClassVar[str] = ""
    WEB_BACKEND: ClassVar[str] = ""

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
            仓库根目录绝对路径；实现委托给 ``app.config.paths.repository_root``。

        异常:
            无。

        副作用:
            无。
        """

        return paths.repository_root()

    @staticmethod
    def _load_local_env(repository_root: Path) -> None:
        """从系统级 ``.cosir`` 配置文件加载未显式设置的环境变量。

        参数:
            repository_root: 保留参数以维持启动调用契约；路径实际由 ``app.utils.paths`` 决定。

        返回:
            无。

        异常:
            OSError: 当 env 文件存在但无法读取时抛出。

        副作用:
            将系统级 ``.cosir/.env`` / ``.env.local`` 中的键值对写入当前进程环境，但不会覆盖已存在的
            环境变量。
        """

        merged_values: dict[str, str] = {}
        del repository_root
        for env_file in paths.env_files():
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
    def load(cls, repository_root: Path | None = None) -> None:
        """加载默认配置与本地 env 覆盖，填充类级静态属性。

        进程启动时调用一次（``__main__.py`` 与 ``app.py`` 的 lifespan 均会调用）；模块导入时
        亦会调用一次，使未显式启动的单元测试也能拿到仓库根推导出的默认路径。

        参数:
            repository_root: 仓库根目录绝对路径；省略时从本文件路径推导。

        返回:
            无。

        异常:
            ValueError: ``CODING_AGENT_LANGFUSE_ENABLED`` 不是受支持的布尔文本时抛出。

        副作用:
            加载 ``.env`` / ``.env.local`` 到进程环境；覆盖本类全部静态属性；经 ``paths.reset``
            按当前环境重新对齐固定路径（数据根 / 日志目录 / 主业务库与 checkpoint 文件）。
        """

        root = repository_root or cls.repository_root()
        cls._load_local_env(root)
        # 固定路径唯一事实源在 ``app.utils.paths``：加载 .env 后按环境重新对齐，使
        # 系统 ``.cosir/.env`` 中的运行配置在此阶段生效；路径根由桌面宿主注入。
        paths.reset()
        cls.DEFAULT_LANGUAGE = os.environ.get("CODING_AGENT_DEFAULT_LANGUAGE", "zh").strip().lower()
        cls.WEB_SEARCH_BACKEND = (
            os.environ.get("CODING_AGENT_WEB_SEARCH_BACKEND", "").strip().lower()
        )
        cls.WEB_EXTRACT_BACKEND = (
            os.environ.get("CODING_AGENT_WEB_EXTRACT_BACKEND", "").strip().lower()
        )
        cls.WEB_BACKEND = os.environ.get("CODING_AGENT_WEB_BACKEND", "").strip().lower()

        # Langfuse 可观测性配置（缺省关闭，显式开启且仅在密钥齐备时生效）。
        cls.LANGFUSE_ENABLED = cls._env_bool("CODING_AGENT_LANGFUSE_ENABLED", False)
        cls.LANGFUSE_PUBLIC_KEY = os.environ.get("CODING_AGENT_LANGFUSE_PUBLIC_KEY")
        cls.LANGFUSE_SECRET_KEY = os.environ.get("CODING_AGENT_LANGFUSE_SECRET_KEY")
        cls.LANGFUSE_BASE_URL = os.environ.get(
            "CODING_AGENT_LANGFUSE_BASE_URL",
            "http://124.220.55.187",
        )

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


# 类定义结束后推导可覆盖字段集合，再加载一次默认配置（含 ``.env`` 与环境变量覆盖，并触发
# ``paths.reset()`` 使固定路径与环境对齐）；生产启动时再次调用为幂等覆盖。
Settings._finalize_overridable()
Settings.load()
