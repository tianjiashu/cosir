"""后端全局不可变共享常量（类级命名空间）。

单一职责：集中存放后端各域「不可变、跨模块共享」的命名常量——数值上限 / 超时 / 计数、
协议与 worker 标识、状态与失败 code、编译后的正则、固定集合等。运行期可由环境变量覆盖的
配置不在此处（见 ``app.config.settings.Settings``）；固定路径也不在此处（唯一事实源
``app.utils.path.system_cosir``）；路径外的其它「逻辑配置」由各自 service / 模块承载。

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

    所有域（终端、Run、LLM、Workflow、系统提示词、附件、cosir、日志记录与日志文件、Web、
    Transport、启动、文本、工具）都作为嵌套类挂在本类之下；调用方统一以
    ``Constant.<域>.<NAME>`` 访问，单一事实源、无重复定义。
    """

    class Terminal:
        """终端会话、Worker 子进程与预览流的共享常量。

        这些上限与标识由终端服务层与 Worker 进程两侧共同遵守：服务侧据此限流、回收与协议握手，
        Worker 子进程据此约束帧 / 队列尺寸并通过环境变量声明自身身份。改动任一项都会同时影响
        服务侧与子进程，需两侧同步评估，不可只改一侧。
        """

        # 单进程同时活跃的终端会话数上限（全局容量闸门，用户新建受它约束）。
        MAX_ACTIVE_SESSIONS: int = 32
        # 每个 Task 保留的已结束会话数（snapshot 与 terminal_list 可见）。
        MAX_ENDED_SESSIONS_PER_TASK: int = 10
        # 全进程已结束会话的总保留上限；每个终态会话仍持有行缓冲与预览缓冲，需整体有界。
        MAX_TERMINAL_HISTORY_SESSIONS: int = 64
        # 单会话原始 PTY 输出（预览重放窗口）的字节上限，超出后最旧输出帧被丢弃。
        MAX_RING_BUFFER_BYTES: int = 1024 * 1024
        # 逻辑行缓冲上限：行数与字符数先到先淘汰最旧已封口行，行号不复用。
        MAX_BUFFER_LINES: int = 10_000
        MAX_BUFFER_CHARS: int = 2 * 1024 * 1024
        # 预览队列最大帧数（防止预览消费者堆积）。
        MAX_STREAM_QUEUE_FRAMES: int = 256
        # 预览队列最大字节数。
        MAX_STREAM_QUEUE_BYTES: int = 1024 * 1024
        # Agent 单次写入终端输入的最大字节数。
        MAX_AGENT_INPUT_BYTES: int = 64 * 1024
        # 用户单帧原始输入的最大字节数（WS input 帧解码后）。
        MAX_RAW_INPUT_BYTES: int = 64 * 1024
        # Worker 单次回传输出块的最大字节数（超出分块）。
        MAX_WORKER_OUTPUT_CHUNK_BYTES: int = 16 * 1024
        # --- Agent 观察与等待 ---
        # 按行读取的默认尾部行数与单次 limit 上限。
        READ_DEFAULT_TAIL: int = 40
        READ_MAX_LIMIT: int = 400
        # 观察文本中单行的最大字符数，超出部分以省略标记代替。
        OBSERVATION_LINE_MAX_CHARS: int = 2000
        # 卡片里命令输出保留的最近字符数；完整输出在日志文件里。每次重新订阅，首帧都要带上全部卡片，
        # 窗口越大重连越慢。
        DISPLAY_OUTPUT_WINDOW_CHARS: int = 256 * 1024
        # terminal_read 条件等待的缺省值：输出静默 500ms，总超时 3s。
        WAIT_DEFAULT_QUIET_MS: int = 500
        WAIT_DEFAULT_TIMEOUT_MS: int = 3000
        # 条件等待只在前台进行、占着工具线程，总超时上限因此要短；
        # 长工作交给 execute_terminal 后台执行。
        WAIT_MAX_TIMEOUT_MS: int = 30_000
        # 事件驱动等待的复查时间片：只用于复查 Run 取消信号，条件判定由 worker 事件唤醒。
        WAIT_CANCEL_CHECK_SECONDS: float = 0.25
        # terminal_stop 等命令进程收尾的时限；到点仍未退出返回受控错误，结论稍后照常回投。
        COMMAND_STOP_WAIT_SECONDS: float = 10.0
        # --- Agent 观察 ---
        # 用户最后一次按键后多久内视为「正在打字」：terminal_list / read 据此提示 user typing。
        USER_TYPING_IDLE_SECONDS: float = 3.0
        # --- Worker 子进程协议与约束 ---
        # 单帧最大字节数（Worker 与服务侧帧编解码的共同上限）。
        MAX_FRAME_BYTES: int = 4 * 1024 * 1024
        # Worker 输入队列最大帧数。
        MAX_INPUT_QUEUE_FRAMES: int = 64
        # Worker 输入队列最大字节数（需容纳一帧 base64 编码的最大原始输入）。
        MAX_INPUT_QUEUE_BYTES: int = 512 * 1024
        # 宿主拉起后端时注入的环境变量键：值是 Terminal Worker 可执行文件路径，后端据此启动 worker。
        WORKER_ENV: str = "CODING_AGENT_TERMINAL_WORKER"

        # --- 交互通道（WebSocket terminal-v2） ---
        # 客户端控制帧最大字节数（需容纳 base64 编码的最大原始输入）。
        MAX_STREAM_CONTROL_BYTES: int = 128 * 1024

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
        # 取消落定的结束原因，按取消信号的来源（``CancelReason``）区分：驱动开始时据此推导「用户已
        # 停止」通知（见 ``drive_notices``），所以必须落库、不能统一写成一个值。
        RUN_END_REASON_USER_CANCELLED: str = "user_cancelled"
        RUN_END_REASON_PARENT_CANCELLED: str = "parent_cancelled"
        # 配置期失败：provider / 模型解析不出可用模型，或 Run 缺少 provider 绑定。
        RUN_FAILURE_CODE_MODEL_CONFIG_UNAVAILABLE: str = "model_resolve_failed"
        # 流程类失败：模型未返回可用内容。
        RUN_FAILURE_CODE_MODEL_OUTPUT_INVALID: str = "invalid_model_output"
        # 单次 Run 达到最大步数上限。
        RUN_FAILURE_CODE_MAX_STEPS: str = "max_steps_reached"
        # workflow 图执行异常。
        RUN_FAILURE_CODE_GRAPH_FAILED: str = "workflow_graph_failed"
        RUN_FAILURE_CODE_CONTEXT_PROTOCOL_INVALID: str = "context_protocol_invalid"
        # workflow 图已被标记为结束，无法继续。
        RUN_FAILURE_CODE_GRAPH_ALREADY_FINISHED: str = "graph_already_finished"
        # 后端重启导致本轮对话中断。
        RUN_FAILURE_CODE_BACKEND_RESTARTED: str = "backend_restarted"
        # 该 Run 正在等待用户决定，却被不带决定的续跑拉去跑模型（会静默丢掉待决请求）。
        # 未知 code 的统一兜底用户文案。
        DEFAULT_MESSAGE: str = "对话运行失败，请重试或查看日志"

    class LLM:
        """模型请求的全局参数：所有模型统一，Agent 级 ``ModelSettings`` **无**对应覆盖字段。"""

        # 单次模型 HTTP 请求超时（秒）；覆盖 SDK 默认的 600s，以便更快失败并重试。
        REQUEST_TIMEOUT_SECONDS: float = 120.0
        # 模型目录发现的 HTTP 超时（秒）；目录请求只用于填充表单，不应阻塞配置弹窗过久。
        MODEL_DISCOVERY_TIMEOUT_SECONDS: float = 8.0
        # SDK 层失败重试上限（不含超时本身的首次尝试）；0 表示不重试。
        MAX_RETRIES: int = 2
        # 生成种子：None 表示不固定种子（由 API 随机，正常生产语义）；设为非负整数可让输出
        # 可复现，仅用于本地调试。迁移前由 ``CODING_AGENT_LLM_SEED`` 覆盖，现已固化为常量
        # ——需要复现时直接改本值。
        SEED: int | None = None

    class Workflow:
        """ReAct loop 的共享常量。"""

        # Provider 归一化后表示「正常结束、无需续写」的 finish_reason 集合。
        NORMAL_FINISH_REASONS: frozenset[str] = frozenset({"stop", "end", "end_turn"})
        # 工具参数非法时，回传给模型的参数预览截断字符数。
        INVALID_TOOL_ARGS_PREVIEW_CHARS: int = 500
        # 单次工具调用错误累计展示的字符预算上限。
        INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS: int = 2000
        # 结构化输出阶段的模型请求总次数上限，包含首次请求与后续重试。
        STRUCTURED_OUTPUT_MAX_ATTEMPTS: int = 4
        # 已完成 Run 的结构化支线失败原因；不改变 Run 生命周期状态。
        STRUCTURED_OUTPUT_FAILURE_REASON: str = "structured_output_failed"
        # 标题支线从启动起的总时限（秒）：Run 收尾最多等到这里，超时即取消，保留原标题。
        TASK_TITLE_TIMEOUT_SECONDS: float = 20.0
        # 流式文本刷新最小字符数（达到后再 flush，减少碎片）。
        DEFAULT_TEXT_FLUSH_MIN_CHARS: int = 32
        # 流式文本刷新最大间隔（秒）。
        DEFAULT_TEXT_FLUSH_MAX_INTERVAL_SECONDS: float = 0.05
        # 步数耗尽时写给父 Agent / 用户的默认可见英文说明（无最终回答时的明确失败原因）。
        MAX_STEPS_FINAL_TEXT: str = (
            "The agent stopped after reaching the maximum number of steps "
            "before producing a final answer."
        )
        # 领域用量 metadata 缓存读明细键名（``input_token_details["cache_read"]``）。
        INPUT_CACHE_READ_KEY: str = "cache_read"
        # 领域用量 metadata 推理明细键名（``output_token_details["reasoning"]``）。
        OUTPUT_REASONING_KEY: str = "reasoning"

    class SystemPrompt:
        """系统提示词各层预算闸门（原先散落在 ``Settings`` 与 builder 模块内的数值）。

        Agent 系统预设、全局指令、Workspace 指令三个提示词内容层各有一个 token 上限，决定该层最多
        注入多少内容；超限时由 ``SystemPromptBuilder`` 按 token 估算截断前缀。所有数值都是固定常量，
        不由环境变量覆盖（历史 env 覆盖面从未被使用）。
        """

        # Layer 2（Agent 系统预设）token 上限。
        AGENT_PERSONA_MAX_TOKENS: int = 2_000
        # Layer G（系统级全局指令）token 上限。
        GLOBAL_INSTRUCTION_MAX_FILE_TOKENS: int = 10_000
        # Layer 3（Workspace 项目指令）token 上限。
        WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS: int = 10_000

    class Attachment:
        """附件上传与图片归一化的共享常量。"""

        # 单张原始上传图片的流式字节上限。
        MAX_UPLOAD_BYTES: int = 20 * 1024 * 1024
        # 单张规范化后图片的最终存储字节上限。
        MAX_IMAGE_BYTES: int = 20 * 1024 * 1024
        # 产品统一支持的图片格式；上传阶段只接受 JPEG 与 PNG。
        IMAGE_FORMATS: frozenset[str] = frozenset({"jpeg", "png"})
        # 一次模型请求最多携带的图片数量。
        MAX_IMAGES_PER_REQUEST: int = 20
        # 一次模型请求中所有图片的总字节上限。
        REQUEST_TOTAL_MAX_BYTES: int = 48 * 1024 * 1024
        # 单张图片允许的最长边像素数。
        MAX_IMAGE_SIDE_PX: int = 8192
        # 资源 ID 格式：64 位小写十六进制。
        ASSET_ID: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")
        # 资源文件名格式：``<asset_id>.<extension>``。
        ASSET_FILE: re.Pattern[str] = re.compile(
            r"^(?P<asset_id>[0-9a-f]{64})\.(?P<extension>jpeg|png)$"
        )
        # 图片 MIME 类型映射（格式 -> content-type），以只读映射展示。
        CONTENT_TYPES: ClassVar[Mapping[str, str]] = {
            "jpeg": "image/jpeg",
            "png": "image/png",
        }

    class LogRecord:
        """结构化日志记录的共享常量。"""

        # 单条映射日志文本的最大长度（超出截断，避免单条日志撑爆存储）。
        MAX_LOG_TEXT_LENGTH: int = 2000
        # 事件名合法格式：小写字母开头，后接小写字母 / 数字 / 下划线。
        EVENT_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]*$")

    class Logging:
        """日志文件落盘的轮转参数（记录级上限见 ``LogRecord``）。

        由 ``app/config/logging`` 的 size 轮转 handler 与启动编排共同遵守：单个文件超过
        ``MAX_BYTES`` 后轮转为 ``.N`` 分片，最多保留 ``BACKUP_COUNT`` 份历史分片。
        """

        # 单个日志文件最大字节数（超出轮转）。
        MAX_BYTES: int = 5 * 1024 * 1024
        # 历史分片保留数量上限。
        BACKUP_COUNT: int = 7

    class Web:
        """网页搜索 / 提取工具的共享常量。"""

        # 单次 web_search 最多返回的结果条数（同时是模型可见 schema 的 ``limit`` 上限）。
        SEARCH_LIMIT_MAX: int = 20
        # web_search ``limit`` 的默认值：模型可见 schema 默认值与 handler 形参默认值同源。
        SEARCH_LIMIT_DEFAULT: int = 5
        # 单次 web_extract 最多接受的 URL 数（同时是模型可见 schema 的 ``urls`` 长度上限）。
        EXTRACT_URL_LIMIT_MAX: int = 5
        # web_extract 单次返回正文的字符上限（调用方未显式传 char_limit 时的默认值）。
        EXTRACT_CHAR_LIMIT: int = 15_000
        # Markdown 内联 base64 图片占位符提取正则（忽略大小写）。
        BASE64_IMAGE_PATTERN: re.Pattern[str] = re.compile(
            r"!\[([^\]]*)\]\(data:image/[A-Za-z0-9.+-]+;base64,[^)]*\)",
            re.IGNORECASE,
        )
        # 单次 URL 拦截日志最多记录的解析地址数（防日志字段过长）。
        MAX_LOGGED_ADDRESSES: int = 8
        # 本地代理 fake-ip 占位网段（RFC2544 保留，永不公网路由），从内网拦截中豁免。
        PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
        # 响应 / URL 中疑似密钥的脱敏匹配正则。
        SECRET_VALUE_RE: re.Pattern[str] = re.compile(
            r"(?i)(sk-[a-z0-9_-]{8,}|xox[baprs]-[a-z0-9-]{8,}|gh[pousr]_[a-z0-9_]{12,}|"
            r"api[_-]?key[=:][^&\s]+|bearer\s+[a-z0-9._-]{12,})"
        )
        # Web provider 单次 HTTP 请求超时（秒）：web_search / web_extract 的工具级超时在此
        # 之上派生（``+5`` / ``+10``），firecrawl provider 的直连请求也用它。
        # 取值沿用迁移前 ``CODING_AGENT_WEB_REQUEST_TIMEOUT_SECONDS`` 的实际生效值（抓取较慢
        # 时放宽），迁移后不再支持 env 覆盖——需要调整直接改本值。
        REQUEST_TIMEOUT_SECONDS: float = 45.0
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
        """Assistant Transport 层共享常量：cosir 资源定位符与历史窗口。"""

        # 图片资源定位符：``cosir-attachment://<64hex>``。
        IMAGE_LOCATOR: re.Pattern[str] = re.compile(r"^cosir-attachment://[0-9a-f]{64}$")
        # 完整快照与每页更早历史大约带多少条消息（起点再往前对齐到一条用户消息）。长对话里每次
        # 打开、重连与重新同步只传最近这一段，更早的历史随用户往上翻按页取。
        HISTORY_WINDOW_MESSAGES: int = 100

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
        WORKDIR_SAFE_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9/\\:_\-.~ +=@,]+$")
        # 解释器 ``-c/-e/-r/-Command`` 代码串提取正则（跨多行，引号内非贪婪捕获）。
        INTERPRETER_CODE_RE: re.Pattern[str] = re.compile(
            r"\b(?:python(?:[0-9.]*)?|node(?:js)?|ruby|perl|php|bash|sh|zsh|fish|pwsh|powershell)\s+"
            r"(?:-[cer]\b|-Command\b)\s*"
            r"(['\"])(.*?)(?<!\\)\1",
            re.IGNORECASE | re.DOTALL,
        )
        # 工具结果进入模型通道的 content 字符上限（超出由 ``ToolOutputBudget`` 截断）。
        MAX_OUTPUT_CHARS: int = 20_000
        # 同一回复内工具调用的并行 worker 上限（并行批次按此收敛，超出的调用排队执行）。
        MAX_PARALLEL_CALLS: int = 8
