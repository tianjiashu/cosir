/**
 * 工具目录：每个工具在界面上叫什么（动词）、用什么图标、调用行标题怎么从参数拼。
 *
 * 这是工具展示文案的唯一事实源。后端下发呈现类别、`toolName`、`args` 与原始模型 `intent`：工具名是封闭集合，
 * 标签、图标与 i18n 都属于客户端；后端注册表守卫（`test_tool_registry_display_guard.py`）按工具名核对
 * 本表，新增工具漏了条目就会失败。条目以两个空格缩进的 `tool_name: {` 开头，守卫按这个形状提取。
 *
 * 按工具名查表是声明式的，允许；按工具名写渲染分支仍然禁止——展开体与元信息只按 `display_data.kind`
 * 选视图（`call-view-registry.ts`）。
 *
 * 参数不可信：`args` 是模型给的原样参数，排队（pending）时为空对象，所以每个取值都要容忍缺字段，取不到就
 * 没有目标、标题回落到动词。调用标题优先使用统一 `intent`，它不参与状态或结果判断。
 */

/** 调用行标题：意图 + 目标，如 `读取文件` + `src/a.ts`；调用标题优先展示模型写的 `intent`，命令原文归展开体。 */
export type ToolHeading = {
  title: string;
  target: string | null;
};

type ToolArgs = Record<string, unknown>;

type ToolCatalogEntry = {
  /** 工具动词：区头按它计数，也是没有自定义标题时的标题。 */
  verb: string;
  /** lucide 图标名，取值见 `tool-icons.tsx`。 */
  icon: string;
  /** 目标（路径、模式、会话、子 Agent……）；没有目标或参数还没到时为 null。 */
  target?: (args: ToolArgs) => string | null;
  /** 意图尚未到达时的实体名称回退（子 Agent 名等）；否则回落到动词。 */
  title?: (args: ToolArgs) => string | null;
};

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

/** 多行输入（命令、脚本、补丁）只取首个非空行。 */
function firstLine(value: unknown): string | null {
  const text = str(value);
  if (text === null) return null;
  return text.split(/\r?\n/).map((line) => line.trim()).find((line) => line !== "") ?? null;
}

/** 多目标调用只写首个目标，其余用 `+N` 表示。 */
function withMore(values: unknown): string | null {
  if (!Array.isArray(values) || values.length === 0) return null;
  const first = str(values[0]);
  if (first === null) return null;
  return values.length > 1 ? `${first} +${values.length - 1}` : first;
}

/** 长文本（脚本表达式）只取开头一段，超出用省略号。 */
function clipped(value: string | null, max: number): string | null {
  return value !== null && value.length > max ? `${value.slice(0, max)}…` : value;
}

function joined(...parts: (string | null)[]): string | null {
  return parts.every((part) => part !== null) ? parts.join("") : null;
}

const path = (args: ToolArgs) => str(args.path);
const terminalId = (args: ToolArgs) => str(args.terminal_id);
const sessionRef = (args: ToolArgs) => (typeof args.session_id === "number" ? `#${args.session_id}` : null);

const PATCH_FILE_HEADER = /^diff --git a\/(\S+) b\//gm;

function patchTargets(args: ToolArgs): string | null {
  const patch = str(args.patch);
  if (patch === null) return null;
  const paths = [...new Set([...patch.matchAll(PATCH_FILE_HEADER)].map((match) => match[1]))];
  return withMore(paths);
}

/** 填入终端：`终端 · 要填入的一行命令`；命令参数还没到时只有终端。 */
function terminalInput(args: ToolArgs): string | null {
  const id = terminalId(args);
  return joined(id, " · ", str(args.command)) ?? id;
}

function mcpTarget(args: ToolArgs): string | null {
  const server = str(args.server_name);
  const tool = str(args.tool_name);
  return args.action === "call" && server && tool ? `${server}/${tool}` : server;
}

function skillTarget(args: ToolArgs): string | null {
  const skill = str(args.skill_name);
  const resource = str(args.resource_path);
  return skill && resource ? `${skill}/${resource}` : skill;
}

/** 提问的目标：第一题的短标签，多题时带 `+N`。 */
function questionHeaders(args: ToolArgs): string | null {
  if (!Array.isArray(args.questions)) return null;
  return withMore(args.questions.map((question) => (question && typeof question === "object" ? (question as ToolArgs).header : null)));
}

/** `browser_act` 动作类型 → 动词：调用行的目标与卡片的每一步共用这一张表。 */
const BROWSER_ACTION_VERBS: Readonly<Record<string, string>> = {
  click: "点击", hover: "悬停", drag: "拖动", type: "输入", press: "按键", select: "选择",
  scroll: "滚动", upload: "上传", wait_for: "等待", dialog: "回答弹窗",
};

/** 未知的动作类型（后端新增而这里还没跟上）照原样显示。 */
export function browserActionVerb(type: string): string {
  return BROWSER_ACTION_VERBS[type] ?? type;
}

function browserActTarget(args: ToolArgs): string | null {
  if (!Array.isArray(args.actions) || args.actions.length === 0) return null;
  const first = args.actions[0] as ToolArgs | null;
  const type = first && typeof first === "object" ? str(first.type) : null;
  if (type === null) return null;
  const verb = browserActionVerb(type);
  return args.actions.length > 1 ? `${verb} +${args.actions.length - 1}` : verb;
}

export const TOOL_CATALOG: Record<string, ToolCatalogEntry> = {
  read_file: { verb: "读取文件", icon: "eye", target: path },
  search_content: { verb: "搜索内容", icon: "search", target: (args) => joined('"', str(args.pattern), '" in ', str(args.path)) },
  find_files: { verb: "查找文件", icon: "search", target: (args) => joined(str(args.pattern), " in ", str(args.path)) },
  list_directory: { verb: "查看目录", icon: "eye", target: path },
  read_image: { verb: "查看图片", icon: "eye", target: (args) => withMore(args.paths) },
  write_file: { verb: "写入文件", icon: "git-compare", target: path },
  patch_write: { verb: "替换文本", icon: "git-compare", target: path },
  apply_patch: { verb: "应用补丁", icon: "git-compare", target: patchTargets },
  execute_terminal: { verb: "执行命令", icon: "terminal" },
  terminal_list: { verb: "列出终端", icon: "terminal" },
  terminal_read: { verb: "读取终端", icon: "terminal", target: terminalId },
  terminal_write: { verb: "填入终端", icon: "terminal", target: terminalInput },
  terminal_stop: { verb: "停止命令", icon: "x", target: terminalId },
  browser_navigate: { verb: "打开网页", icon: "globe", target: (args) => str(args.url) ?? str(args.action) },
  browser_snapshot: { verb: "读取网页", icon: "globe", target: (args) => str(args.tab) },
  browser_act: { verb: "操作网页", icon: "globe", target: browserActTarget },
  browser_screenshot: { verb: "网页截图", icon: "globe", target: (args) => (args.full_page === true ? "full page" : str(args.tab)) },
  browser_tabs: { verb: "管理标签页", icon: "globe", target: (args) => (str(args.tab) ? joined(str(args.action), " ", str(args.tab)) : str(args.action)) },
  browser_logs: { verb: "读取网页日志", icon: "globe", target: (args) => str(args.kind) },
  browser_evaluate: { verb: "执行网页脚本", icon: "globe", target: (args) => clipped(firstLine(args.expression), 60) },
  browser_handoff: { verb: "交给你操作网页", icon: "globe", target: (args) => str(args.reason) },
  web_search: { verb: "网页搜索", icon: "globe", target: (args) => str(args.query) },
  web_extract: { verb: "网页正文提取", icon: "globe", target: (args) => withMore(args.urls) },
  mcp: { verb: "调用 MCP", icon: "network", target: mcpTarget },
  skill: { verb: "加载 Skill", icon: "puzzle", target: skillTarget },
  delegate_task: { verb: "委派任务", icon: "users", title: (args) => str(args.agent_name), target: (args) => str(args.child_agent_id) },
  fork_session: { verb: "分叉会话", icon: "git-fork", title: (args) => str(args.title) },
  send_message: { verb: "发送消息", icon: "send", target: sessionRef },
  session_status: { verb: "读取会话状态", icon: "info", target: sessionRef },
  wait_messages: { verb: "等待结果", icon: "clock" },
  ask_user: { verb: "向你提问", icon: "circle-help", target: questionHeaders },
  propose_agent_configuration: { verb: "生成子 Agent 配置草稿", icon: "bot", target: (args) => str(args.agent_id) },
};

/** 工具动词；目录里没有的工具（后端新增了但前端未登记）退回工具名，图标退回扳手。 */
export function toolVerb(toolName: string): string {
  return TOOL_CATALOG[toolName]?.verb ?? toolName;
}

export function toolIconName(toolName: string): string | undefined {
  return TOOL_CATALOG[toolName]?.icon;
}

/** 调用行标题：自定义意图取不到时用动词；目标取不到时为 null。 */
export function toolHeading(toolName: string, args: ToolArgs | null | undefined, intent?: string | null): ToolHeading {
  const entry = TOOL_CATALOG[toolName];
  const parameters = args ?? {};
  if (entry === undefined) return { title: intent ?? toolName, target: null };
  return {
    title: intent ?? entry.title?.(parameters) ?? entry.verb,
    target: entry.target?.(parameters) ?? null,
  };
}
