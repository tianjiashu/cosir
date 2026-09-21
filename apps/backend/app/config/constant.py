"""后端全局不可变共享常量（类级命名空间）。

单一职责：集中存放后端各域「不可变、跨模块共享」的命名常量——数值上限 / 超时 / 计数、
协议与 worker 标识、状态与失败 code、编译后的正则、固定集合等。运行期可由环境变量覆盖的
配置不在此处（见 ``app.config.settings.Settings``）；固定路径也不在此处（唯一事实源
``app.utils.paths``）；路径外的其它「逻辑配置」由各自 service / 模块承载。

设计约定：
- 调用一律通过 ``Constant.<域>.<NAME>``（如 ``Constant.Terminal.MAX_ACTIVE_SESSIONS``），
  禁止在调用点再写一遍字面量或自建同名常量，避免多份事实源漂移。
- 本模块为叶子模块，只依赖标准库（``re`` / ``ipaddress``），不得 import 任何 ``app`` 子模块，
  避免循环依赖；凡引用 ``app`` 枚举值之处一律展开为基础字面量（如 Run 状态 ``"completed"``）。
- 常量全部为模块级不可变值（``int`` / ``str`` / ``frozenset`` / 编译后的正则），新增常量需带
  中文注释，说明语义、取值依据与影响范围。
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import ClassVar


class Constant:
    """后端各域不可变共享常量的统一命名空间。

    所有域（终端、委派、Run、Workflow、附件、cosir、日志、Web、Transport、启动、文本、工具）
    都作为嵌套类挂在本类之下；调用方统一以 ``Constant.<域>.<NAME>`` 访问，单一事实源、无重复定义。
    """

    class Terminal:
        """终端会话、Worker 子进程与预览流的共享常量。

        这些上限与标识由终端服务层与 Worker 进程两侧共同遵守：服务侧据此限流、回收与协议握手，
        Worker 子进程据此约束帧 / 队列尺寸并通过环境变量声明自身身份。改动任一项都会同时影响
        服务侧与子进程，需两侧同步评估，不可只改一侧。
        """

        # 单进程同时活跃的终端会话数上限（全局容量闸门，超出则新建会话被拒）。
        MAX_ACTIVE_SESSIONS: int = 32
        # 终态 session 只保留有限的查询历史，避免 registry/幂等状态随运行时间增长。
        MAX_TERMINAL_HISTORY_SESSIONS: int = 256
        # 单会话环形缓冲字节上限，超出后最旧输出被丢弃。
        MAX_RING_BUFFER_BYTES: int = 1024 * 1024
        # 预览队列最大帧数（防止预览消费者堆积）。
        MAX_PREVIEW_QUEUE_FRAMES: int = 256
        # 预览队列最大字节数。
        MAX_PREVIEW_QUEUE_BYTES: int = 1024 * 1024
        # Agent 单次写入终端输入的最大字节数。
        MAX_AGENT_INPUT_BYTES: int = 64 * 1024
        # Worker 单次回传输出块的最大字节数（超出分块）。
        MAX_WORKER_OUTPUT_CHUNK_BYTES: int = 16 * 1024
        # 会话默认最长存活时长（秒），超时由 sweeper 回收。
        DEFAULT_MAX_LIFETIME_SECONDS: int = 8 * 60 * 60
        # 会话默认空闲超时（秒），无活动超过该值由 sweeper 回收。
        DEFAULT_IDLE_TIMEOUT_SECONDS: int = 30 * 60
        # sweeper 扫描闲置会话的周期间隔（秒）。
        SWEEPER_INTERVAL_SECONDS: int = 30

        # --- Worker 子进程协议与约束 ---
        # 单帧最大字节数（Worker 与服务侧帧编解码的共同上限）。
        MAX_FRAME_BYTES: int = 4 * 1024 * 1024
        # Worker 输入队列最大帧数。
        MAX_INPUT_QUEUE_FRAMES: int = 64
        # Worker 输入队列最大字节数。
        MAX_INPUT_QUEUE_BYTES: int = 64 * 1024
        # 已完成 terminal_write 幂等结果的进程内上限；session 终态时会按 session 清理。
        MAX_WRITE_OPERATION_CACHE: int = 4096
        # 服务侧与 Worker 握手时约定的协议版本标识。
        WORKER_PROTOCOL: str = "terminal-worker-v2"
        # Worker 子进程启动时注入的环境变量键，用于标识自身为终端 worker。
        WORKER_ENV: str = "CODING_AGENT_TERMINAL_WORKER"

        # --- 预览流（HTTP / WebSocket） ---
        # 预览控制帧最大字节数。
        MAX_PREVIEW_CONTROL_BYTES: int = 8 * 1024
        # 预览轮询间隔（秒）。
        PREVIEW_POLL_SECONDS: float = 0.25

    class Delegation:
        """委派子 Agent 并发执行的共享常量。"""

        # 视为「活跃」的 delegation 状态集合，用于并发额度统计与活跃列表查询。
        ACTIVE_DELEGATION_STATUSES: tuple[str, ...] = ("pending", "running")
        # 并发额度不足时 ``DelegationAcquireResult.reason`` 的唯一取值。
        REASON_CONCURRENCY_EXCEEDED: str = "delegation_concurrency_exceeded"

    class Run:
        """Conversation Run 失败 / 终态相关的稳定 code 与状态集合。

        单一事实源：所有 Run 失败 code 只在此声明一次，``conversation_runs.end_reason`` 与
        ``ConversationRunError.code`` 共用同一组值，避免两处漂移。模型调用错误的分类词表是
        ``ErrorKind``（``app.models.enums.error_kind``），本模块不再另行定义 ``model_*`` 系列 code。
        """

        # 兜底失败 code：没有任何可用分类信息时使用。
        RUN_FAILURE_CODE_UNKNOWN: str = "run_failed"
        # 取消类终态兜底 code：领域侧未给出具体取消原因时使用。
        RUN_FAILURE_CODE_CANCELLED: str = "run_cancelled"
        # 配置期失败：provider / 模型解析不出可用模型，或 Run 缺少 provider 绑定。
        RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE: str = "model_resolve_failed"
        # 流程类失败：模型未返回可用内容。
        RUN_FAILURE_CODE_MODEL_OUTPUT_INVALID: str = "invalid_model_output"
        # 连续工具调用失败次数触顶。
        RUN_FAILURE_CODE_TOOL_ERROR_LIMIT: str = "tool_error_limit_reached"
        # 单次 Run 达到最大步数上限。
        RUN_FAILURE_CODE_MAX_STEPS: str = "max_steps_reached"
        # workflow 图执行异常。
        RUN_FAILURE_CODE_GRAPH_FAILED: str = "workflow_graph_failed"
        # workflow 图已被标记为结束，无法继续。
        RUN_FAILURE_CODE_GRAPH_ALREADY_FINISHED: str = "graph_already_finished"
        # 后端重启导致本轮对话中断。
        RUN_FAILURE_CODE_BACKEND_RESTARTED: str = "backend_restarted"
        # 客户端连接断开导致本轮对话中断。
        RUN_FAILURE_CODE_CLIENT_DISCONNECTED: str = "client_disconnected"
        # 未知 code 的统一兜底用户文案。
        DEFAULT_MESSAGE: str = "对话运行失败，请重试或查看日志"

        # 视为「终态」的 Run 状态集合（用于识别 terminal 类 run 是否已结束，取值来自
        # ``ConversationRunStatus`` 的稳定字符串值）。
        TERMINAL_STATUSES: frozenset[str] = frozenset(
            {"completed", "failed", "cancelled"}
        )

    class Workflow:
        """LangGraph React workflow 的共享常量。"""

        # Provider 归一化后表示「正常结束、无需续写」的 finish_reason 集合。
        NORMAL_FINISH_REASONS: frozenset[str] = frozenset({"stop", "end", "end_turn"})
        # 表示「因长度受限截断、需续写」的 finish_reason 集合。
        CONTINUATION_FINISH_REASONS: frozenset[str] = frozenset(
            {"length", "max_tokens", "max_output_tokens"}
        )
        # 工具参数非法时，回传给模型的参数预览截断字符数。
        INVALID_TOOL_ARGS_PREVIEW_CHARS: int = 500
        # 单次工具调用错误累计展示的字符预算上限。
        INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS: int = 2000
        # 流式文本刷新最小字符数（达到后再 flush，减少碎片）。
        DEFAULT_TEXT_FLUSH_MIN_CHARS: int = 32
        # 流式文本刷新最大间隔（秒）。
        DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS: float = 0.05
        # 步数耗尽时写给父 Agent / 用户的默认可见英文说明（无最终回答时的明确失败原因）。
        MAX_STEPS_FINAL_TEXT: str = (
            "The agent stopped after reaching the maximum number of steps "
            "before producing a final answer."
        )
        # LangChain UsageMetadata 缓存读明细键名（``input_token_details["cache_read"]``）。
        INPUT_CACHE_READ_KEY: str = "cache_read"
        # LangChain UsageMetadata 推理明细键名（``output_token_details["reasoning"]``）。
        OUTPUT_REASONING_KEY: str = "reasoning"

    class Attachment:
        """附件上传与图片归一化的共享常量。"""

        # 单附件最大上传字节数。
        MAX_UPLOAD_BYTES: int = 32 * 1024 * 1024
        # 支持的图片格式集合。
        IMAGE_FORMATS: frozenset[str] = frozenset(
            {"jpeg", "png", "gif", "webp", "bmp", "tiff"}
        )
        # 资源 ID 格式：64 位小写十六进制。
        ASSET_ID: "re.Pattern[str]" = re.compile(r"^[0-9a-f]{64}$")
        # 资源文件名格式：``<asset_id>[.source].<extension>``。
        ASSET_FILE: "re.Pattern[str]" = re.compile(
            r"^(?P<asset_id>[0-9a-f]{64})(?:\.source)?\.(?P<extension>[A-Za-z0-9]+)$"
        )
        # 图片 MIME 类型映射（格式 -> content-type），以只读映射展示。
        CONTENT_TYPES: ClassVar[Mapping[str, str]] = {
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
            "bmp": "image/bmp",
            "tiff": "image/tiff",
        }

    class Cosir:
        """cosir 内部文件 / 图片占位 token 的正则（多处复用，单一事实源）。"""

        # 本地文件占位 token：``[[cosir-file:<id>]]``。
        LOCAL_FILE_TOKEN: "re.Pattern[str]" = re.compile(r"\[\[cosir-file:([^\]]+)\]\]")
        # 本地图片占位 token：``[[cosir-image:<id>]]``。
        LOCAL_IMAGE_TOKEN: "re.Pattern[str]" = re.compile(r"\[\[cosir-image:([^\]]+)\]\]")
        # 本地文件 ID 格式：1~128 位安全字符。
        LOCAL_FILE_ID: "re.Pattern[str]" = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

    class LogRecord:
        """结构化日志记录的共享常量。"""

        # 单条映射日志文本的最大长度（超出截断，避免单条日志撑爆存储）。
        MAX_LOG_TEXT_LENGTH: int = 2000
        # 事件名合法格式：小写字母开头，后接小写字母 / 数字 / 下划线。
        EVENT_NAME_PATTERN: "re.Pattern[str]" = re.compile(r"^[a-z][a-z0-9_]*$")

    class Web:
        """网页搜索 / 提取工具的共享常量。"""

        # 兼容期 provider 优先级（仅 firecrawl）。
        LEGACY_PROVIDER_PRIORITY: tuple[str, ...] = ("firecrawl",)
        # Markdown 内联 base64 图片占位符提取正则（忽略大小写）。
        BASE64_IMAGE_PATTERN: "re.Pattern[str]" = re.compile(
            r"!\[([^\]]*)\]\(data:image/[A-Za-z0-9.+-]+;base64,[^)]*\)",
            re.IGNORECASE,
        )
        # 单次 URL 拦截日志最多记录的解析地址数（防日志字段过长）。
        MAX_LOGGED_ADDRESSES: int = 8
        # 本地代理 fake-ip 占位网段（RFC2544 保留，永不公网路由），从内网拦截中豁免。
        PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
        # 响应 / URL 中疑似密钥的脱敏匹配正则。
        SECRET_VALUE_RE: "re.Pattern[str]" = re.compile(
            r"(?i)(sk-[a-z0-9_-]{8,}|xox[baprs]-[a-z0-9-]{8,}|gh[pousr]_[a-z0-9_]{12,}|"
            r"api[_-]?key[=:][^&\s]+|bearer\s+[a-z0-9._-]{12,})"
        )
        # URL query 中需脱敏的敏感键集合。
        SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset(
            {
                "access_token",
                "api_key",
                "apikey",
                "auth",
                "authorization",
                "client_secret",
                "code",
                "id_token",
                "jwt",
                "key",
                "password",
                "refresh_token",
                "secret",
                "session",
                "sig",
                "signature",
                "token",
            }
        )
        # Firecrawl 官方当前 API 版本基址（v2）。
        FIRECRAWL_DEFAULT_BASE_URL: str = "https://api.firecrawl.dev/v2"
        # 单次提取最多并发抓取的页数。
        FIRECRAWL_MAX_CONCURRENT_SCRAPES: int = 5
        # 回传模型的 metadata 中无价值的体积型键（需剔除）。
        FIRECRAWL_METADATA_NOISE_KEYS: frozenset[str] = frozenset(
            {"rawHtml", "links", "screenshot"}
        )
        # 提取结果中已映射到专有字段、不应重复进 metadata 的键。
        FIRECRAWL_EXTRACT_MAPPED_FIELDS: frozenset[str] = frozenset(
            {"url", "markdown", "html", "rawHtml", "content", "links", "screenshot", "error"}
        )

    class Transport:
        """Assistant Transport 层共享常量：cosir 资源定位符 / 隐藏 token 正则与事件去重窗口。"""

        # 图片资源定位符：``cosir-attachment://<64hex>``。
        IMAGE_LOCATOR: "re.Pattern[str]" = re.compile(r"^cosir-attachment://[0-9a-f]{64}$")
        # 本地文件资源定位符：``cosir-local-file:<id>``。
        FILE_LOCATOR: "re.Pattern[str]" = re.compile(r"^cosir-local-file:[A-Za-z0-9._-]{1,128}$")
        # 用户输入中 cosir file/image token 统一匹配：``[[cosir-(file|image):<id>]]``。
        INPUT_TOKEN: "re.Pattern[str]" = re.compile(r"\[\[cosir-(file|image):([^\]]+)\]\]")
        # 被 HTML 注释包裹的隐藏 cosir token（保留编辑器顺序的同时从渲染文本隐藏）。
        HIDDEN_LOCAL_FILE_TOKEN: "re.Pattern[str]" = re.compile(
            r"<!--\s*(\[\[cosir-(?:file|image):[^\]]+\]\])\s*-->"
        )
        # Conversation event 去重窗口容量：projector 保留最近 N 条 event_id 用于抵御重复投递。
        # 依据：重复投递只发生在同一投递路径的近距离重放；按增量合批口径（32 字符/次）换算，
        # 本窗口约等于 256KB 流式文本，远超任何可能的重放距离。窗口有界 ⇒ 内存上界恒定
        # （约 1MB 量级），且不需要任何按 task 的生命周期清理钩子。
        EVENT_DEDUP_WINDOW: int = 8192

    class Boot:
        """后端启动状态文件（bootstate）的阶段常量。

        启动期由 ``app.__main__`` 与 ``app.lifespan`` 写入，``bootstate`` JSON 的 ``phase``
        字段只取这四个稳定字符串值；读取方（启动状态轮询）也只比对这四个值。
        """

        # 启动进行中（生命周期 yield 前）。
        BOOTING: str = "booting"
        # 应用已就绪（lifespan yield 成功）。
        READY: str = "ready"
        # 启动失败（yield 前异常）。
        FAILED: str = "failed"
        # 应用已优雅停止（关闭期）。
        STOPPED: str = "stopped"

    class Text:
        """文本编码相关的共享常量。"""

        # UTF-8 字节顺序标记字符。
        UTF8_BOM: str = "﻿"
        # UTF-8 BOM 的字节形式（用于二进制写入探测）。
        UTF8_BOM_BYTES: bytes = b"\xef\xbb\xbf"

    class Tools:
        """工具执行相关的共享正则与约束。"""

        # workdir 字符白名单：挡住命令注入式 workdir（含 ``;|&$()`` 等注入字符直接拒绝）。
        WORKDIR_SAFE_RE: "re.Pattern[str]" = re.compile(r"^[A-Za-z0-9/\\:_\-.~ +=@,]+$")
        # 解释器 ``-c/-e/-r/-Command`` 代码串提取正则（跨多行，引号内非贪婪捕获）。
        INTERPRETER_CODE_RE: "re.Pattern[str]" = re.compile(
            r"\b(?:python(?:[0-9.]*)?|node(?:js)?|ruby|perl|php|bash|sh|zsh|fish|pwsh|powershell)\s+"
            r"(?:-[cer]\b|-Command\b)\s*"
            r"(['\"])(.*?)(?<!\\)\1",
            re.IGNORECASE | re.DOTALL,
        )
