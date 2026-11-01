/**
 * 工具目录：每个工具在界面上叫什么（动词）、用什么图标、调用行标题怎么从参数拼。
 *
 * 这是工具展示外壳的唯一事实源。后端只下发 `toolName` 与 `args`，不声明任何展示文案：工具名是封闭集合，
 * 标签、图标与 i18n 都属于客户端；后端注册表守卫（`test_tool_registry_display_guard.py`）按工具名核对
 * 本表，新增工具漏了条目就会失败。条目以两个空格缩进的 `tool_name: {` 开头，守卫按这个形状提取。
 *
 * 按工具名查表是声明式的，允许；按工具名写渲染分支仍然禁止——展开体与元信息只按 `display_data.kind`
 * 选视图（`call-view-registry.ts`）。
 *
 * 参数不可信：`args` 是模型给的原样参数，排队（pending）时为空对象，所以每个取值都要容忍缺字段，取不到就
 * 没有目标、标题回落到动词。只用于展示的参数（如命令的 `description`）也只在这里用于展示。
 */

/** 调用行标题：意图 + 目标，如 `读取文件` + `src/a.ts`；命令则是模型写的 `description` + 命令首行。 */
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
  /** 自定义意图；只有命令（模型写的 `description`）与委派（子 Agent 名）需要，取不到时回落到动词。 */
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

function joined(...parts: (string | null)[]): string | null {
  return parts.every((part) => part !== null) ? parts.join("") : null;
}

const path = (args: ToolArgs) => str(args.path);
const sessionId = (args: ToolArgs) => str(args.session_id);
const childTask = (args: ToolArgs) => (typeof args.child_task_id === "number" ? `#${args.child_task_id}` : null);

const PATCH_FILE_HEADER = /^diff --git a\/(\S+) b\//gm;

function patchTargets(args: ToolArgs): string | null {
  const patch = str(args.patch);
  if (patch === null) return null;
  const paths = [...new Set([...patch.matchAll(PATCH_FILE_HEADER)].map((match) => match[1]))];
  return withMore(paths);
}

function terminalInput(args: ToolArgs): string | null {
  const id = sessionId(args);
  const input = typeof args.input === "object" && args.input !== null ? (args.input as ToolArgs) : null;
  if (id === null || input === null) return id;
  const summary = input.type === "keys" && Array.isArray(input.keys)
    ? input.keys.filter((key) => typeof key === "string").join(" ")
    : input.type === "signal" ? str(input.signal) : str(input.text);
  return summary ? `${id} · ${summary}` : id;
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

function browserActTarget(args: ToolArgs): string | null {
  if (!Array.isArray(args.actions) || args.actions.length === 0) return null;
  const first = args.actions[0] as ToolArgs | null;
  const type = first && typeof first === "object" ? str(first.type) : null;
  if (type === null) return null;
  return args.actions.length > 1 ? `${type} +${args.actions.length - 1}` : type;
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
  delete_file: { verb: "删除文件", icon: "file-x", target: path },
  move_file: { verb: "移动文件", icon: "file-symlink", target: (args) => joined(str(args.source_path), " → ", str(args.destination_path)) },
  execute_terminal: { verb: "执行命令", icon: "terminal", title: (args) => str(args.description), target: (args) => firstLine(args.command) },
  terminal_list: { verb: "列出终端", icon: "terminal" },
  terminal_start: { verb: "启动终端", icon: "terminal", target: (args) => str(args.label) ?? str(args.shell) },
  terminal_read: { verb: "读取终端", icon: "terminal", target: sessionId },
  terminal_write: { verb: "输入终端", icon: "terminal", target: terminalInput },
  terminal_handoff: { verb: "交给用户输入", icon: "keyboard", target: sessionId },
  terminal_close: { verb: "关闭终端", icon: "x", target: sessionId },
  browser_navigate: { verb: "打开网页", icon: "globe", target: (args) => str(args.url) ?? str(args.action) },
  browser_snapshot: { verb: "读取网页", icon: "globe", target: (args) => str(args.tab) },
  browser_act: { verb: "操作网页", icon: "globe", target: browserActTarget },
  browser_screenshot: { verb: "网页截图", icon: "globe", target: (args) => (args.full_page === true ? "full page" : str(args.tab)) },
  browser_tabs: { verb: "管理标签页", icon: "globe", target: (args) => (str(args.tab) ? joined(str(args.action), " ", str(args.tab)) : str(args.action)) },
  browser_handoff: { verb: "交给你操作网页", icon: "globe", target: (args) => str(args.reason) },
  web_search: { verb: "网页搜索", icon: "globe", target: (args) => str(args.query) },
  web_extract: { verb: "网页正文提取", icon: "globe", target: (args) => withMore(args.urls) },
  mcp: { verb: "调用 MCP", icon: "network", target: mcpTarget },
  skill: { verb: "加载 Skill", icon: "info", target: skillTarget },
  delegate_task: { verb: "委派任务", icon: "users", title: (args) => str(args.agent_name), target: (args) => str(args.child_agent_id) },
  child_agent_send: { verb: "发送给子 Agent", icon: "send", target: childTask },
  child_agent_status: { verb: "读取子 Agent 状态", icon: "info", target: childTask },
  wait_messages: { verb: "等待结果", icon: "clock" },
  propose_agent_configuration: { verb: "生成子 Agent 配置草稿", icon: "bot", target: (args) => str(args.agent_id) },
  // 只说「运行」：曾经的 Prepare / 配置口径让模型与用户都把它当成建 Team 的工具，而它只运行已有配置。
  agent_team: { verb: "运行已有 Agent Team", icon: "workflow", target: (args) => str(args.team_id) },
  propose_agent_team_configuration: { verb: "生成 Agent Team 配置草稿", icon: "users", target: (args) => str(args.name) },
};

/** 工具动词；目录里没有的工具（后端新增了但前端未登记）退回工具名，图标退回扳手。 */
export function toolVerb(toolName: string): string {
  return TOOL_CATALOG[toolName]?.verb ?? toolName;
}

export function toolIconName(toolName: string): string | undefined {
  return TOOL_CATALOG[toolName]?.icon;
}

/** 调用行标题：自定义意图取不到时用动词；目标取不到时为 null。 */
export function toolHeading(toolName: string, args: ToolArgs): ToolHeading {
  const entry = TOOL_CATALOG[toolName];
  if (entry === undefined) return { title: toolName, target: null };
  return {
    title: entry.title?.(args) ?? entry.verb,
    target: entry.target?.(args) ?? null,
  };
}
