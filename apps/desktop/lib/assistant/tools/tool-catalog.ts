/**
 * 工具目录：每个工具在界面上叫什么（动词）、用什么图标、调用行标题怎么从参数拼。
 *
 * 这是工具展示文案的唯一事实源。后端下发呈现类别、`toolName` 与 `args`：工具名是封闭集合，
 * 标签、图标与 i18n 都属于客户端；后端注册表守卫（`test_tool_registry_display_guard.py`）按工具名核对
 * 本表，新增工具漏了条目就会失败。条目以两个空格缩进的 `tool_name: {` 开头，守卫按这个形状提取。
 *
 * 按工具名查表是声明式的，允许；按工具名写渲染分支仍然禁止——展开体与元信息只按 `display_data.kind`
 * 选视图（`call-view-registry.ts`）。
 *
 * 参数不可信：`args` 是模型给的原样参数，排队（pending）时为空对象，所以每个取值都要容忍缺字段，取不到就
 * 没有目标、标题回落到动词。参数难读的工具（终端命令、页面脚本）自己带 `intent` 参数，标题优先用它；它不参与
 * 状态或结果判断。
 */
import { baseName } from "@/lib/files/file-name";

/** 调用行标题：动词（或意图）+ 目标，如 `读取文件` + `src/a.ts`；没写意图的命令在目标里露首行、全文归展开体。 */
export type ToolHeading = {
  title: string;
  target: string | null;
  /** 目标是一个工作区路径：调用行按路径显示（省略中间目录、保住文件名）。 */
  targetIsPath: boolean;
};

type ToolArgs = Record<string, unknown>;

type ToolCatalogEntry = {
  /** 工具动词：区头按它计数，也是没有自定义标题时的标题。 */
  verb: string;
  /** lucide 图标名，取值见 `tool-icons.tsx`。 */
  icon: string;
  /** 目标：用户认得的实体（路径、命令首行、子 Agent 名……），不写 id；没有目标或参数还没到时为 null。 */
  target?: (args: ToolArgs) => string | null;
  /** 目标就是 `args.path`（单个工作区路径）。 */
  pathTarget?: true;
  /** 工具参数里有 `intent`（后端 `tools/tool_models/call_intent.intent_field`）：写了就用作标题。 */
  intent?: true;
  /**
   * 没写意图时补上的目标：标题退回动词后一串行会长得一样（十条都叫「执行命令」），用它区分是哪一次；
   * 写了意图时不显示，原文归展开体，不在行与卡片里各出现一次。
   */
  targetWithoutIntent?: (args: ToolArgs) => string | null;
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

/** 填入终端：要填入的那一行命令。终端 id 不是用户认得的名字，而且不指定时由后端挑或新开，不进目标。 */
function terminalInput(args: ToolArgs): string | null {
  return str(args.command);
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
  read_file: { verb: "读取文件", icon: "eye", target: path, pathTarget: true },
  search_content: { verb: "搜索内容", icon: "search", target: (args) => joined('"', str(args.pattern), '" in ', str(args.path)) },
  find_files: { verb: "查找文件", icon: "search", target: (args) => joined(str(args.pattern), " in ", str(args.path)) },
  read_image: { verb: "查看图片", icon: "eye", target: (args) => withMore(args.paths) },
  write_file: { verb: "写入文件", icon: "git-compare", target: path, pathTarget: true },
  edit_file: { verb: "编辑文件", icon: "git-compare", target: path, pathTarget: true },
  apply_patch: { verb: "应用补丁", icon: "git-compare", target: patchTargets },
  execute_terminal: { verb: "执行命令", icon: "terminal", intent: true, targetWithoutIntent: (args) => clipped(firstLine(args.command), 60) },
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
  browser_evaluate: { verb: "执行网页脚本", icon: "globe", intent: true, targetWithoutIntent: (args) => clipped(firstLine(args.expression), 60) },
  web_search: { verb: "网页搜索", icon: "globe", target: (args) => str(args.query) },
  web_fetch: { verb: "抓取网页", icon: "globe", target: (args) => withMore(args.urls) },
  skill: { verb: "加载 Skill", icon: "puzzle", target: skillTarget },
  tool_search: { verb: "加载工具", icon: "puzzle", target: (args) => str(args.query) },
  delegate_task: { verb: "委派任务", icon: "users", target: (args) => str(args.agent_name) ?? str(args.child_agent_id) },
  fork_session: { verb: "分叉会话", icon: "git-fork", target: (args) => str(args.title) },
  session_send: { verb: "发送消息", icon: "send", target: sessionRef },
  session_list: { verb: "列出会话", icon: "users" },
  session_read: { verb: "查看会话进度", icon: "info", target: sessionRef },
  session_stop: { verb: "停止会话", icon: "x", target: sessionRef },
  wait_messages: { verb: "等待结果", icon: "clock" },
  ask_user: { verb: "向你提问", icon: "circle-help", target: questionHeaders },
  present_deliverables: { verb: "交付成果", icon: "file-text", target: (args) => withMore(Array.isArray(args.items) ? args.items.map((item) => (item as { target?: unknown } | null)?.target) : undefined) },
  offer_next_step: { verb: "建议下一步", icon: "circle-help", target: (args) => str(args.ask) },
  propose_agent_configuration: { verb: "生成子 Agent 配置草稿", icon: "bot", target: (args) => str(args.agent_id) },
};

/**
 * MCP 工具不是封闭集合：名字由后端按 `mcp__<服务>__<工具>` 生成（`tools/mcp/mcp_tools.py`），按前缀认成一族，
 * 目标取名字里的「服务/工具」（超长或撞名时末尾带哈希，照样显示）。服务与原工具名的准确值在展示数据里，归展开体。
 */
const MCP_PREFIX = "mcp__";

function catalogEntry(toolName: string): ToolCatalogEntry | undefined {
  const entry = TOOL_CATALOG[toolName];
  if (entry !== undefined || !toolName.startsWith(MCP_PREFIX)) return entry;
  const rest = toolName.slice(MCP_PREFIX.length);
  const split = rest.indexOf("__");
  const target = split > 0 ? `${rest.slice(0, split)}/${rest.slice(split + 2)}` : rest;
  return { verb: "调用 MCP", icon: "network", target: () => target };
}

/** 工具动词；目录里没有的工具（后端新增了但前端未登记）退回工具名，图标退回扳手。 */
export function toolVerb(toolName: string): string {
  return catalogEntry(toolName)?.verb ?? toolName;
}

export function toolIconName(toolName: string): string | undefined {
  return catalogEntry(toolName)?.icon;
}

/**
 * 工具组摘要里提到这次调用的短语（`读取文件 auth.py、db.py` 里的 `auth.py`）。写了意图时就是意图本身，它自带
 * 动作，`standalone` 表示不再接在动词后面；否则是目标，路径只留文件名（全路径在展开的调用行里）。命令、脚本
 * 没写意图时没有短语：原文难读，摘要宁可只写动词或次数。
 */
export type ToolMention = { text: string; standalone: boolean };

/** 摘要里非路径目标（搜索词、网址）的长度上限：摘要一行要放下两三个短语。 */
const MENTION_MAX = 40;

export function toolMention(toolName: string, args: ToolArgs | null | undefined): ToolMention | null {
  const entry = catalogEntry(toolName);
  const parameters = args ?? {};
  if (entry === undefined) return null;
  const intent = entry.intent ? str(parameters.intent)?.trim() ?? null : null;
  if (intent) return { text: intent, standalone: true };
  const target = entry.target?.(parameters) ?? null;
  if (target === null) return null;
  return { text: entry.pathTarget ? baseName(target) : clipped(target, MENTION_MAX)!, standalone: false };
}

/** 调用行标题：带意图的工具写了意图就用它，否则用动词；目标取不到时为 null。 */
export function toolHeading(toolName: string, args: ToolArgs | null | undefined): ToolHeading {
  const entry = catalogEntry(toolName);
  const parameters = args ?? {};
  if (entry === undefined) return { title: toolName, target: null, targetIsPath: false };
  const intent = entry.intent ? str(parameters.intent)?.trim() ?? null : null;
  return {
    title: intent ?? entry.verb,
    target: entry.target?.(parameters) ?? (intent ? null : entry.targetWithoutIntent?.(parameters)) ?? null,
    targetIsPath: entry.pathTarget === true,
  };
}
