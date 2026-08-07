/**
 * 工具展示渲染规则层（客户端唯一渲染事实源）。
 *
 * 后端只产出「静态展示声明 + 结构化数据」，本模块负责把它们投影成客户端可直接
 * 渲染的摘要与条目：折叠态请求摘要、执行后结果摘要、list 布局条目、diff 条目。
 *
 * 边界：
 * - 纯函数，无副作用、不抛异常，字段缺失一律安全降级。
 * - 按「参数 / 数据形状」投影，而非按工具名写渲染分支；工具名仅用于选择规则条目，
 *   规则本身集中在本模块的规则表内，新增工具只在表里加一条，不散落到组件。
 *
 * @module shared/toolDisplayRules
 */

/** 结构化数据字典别名。 */
export type ToolDataRecord = Record<string, unknown>;

/** list 布局条目（客户端按字段形状渲染）。 */
export interface ToolListEntry {
  /** 条目主名称，通常为文件名。 */
  name: string;
  /** 条目所在路径（父目录或搜索根）。 */
  path: string;
  /** 条目类型：`dir` / `file`；缺省视为 file。 */
  type?: string;
  /** 内容命中所在文件路径（内容搜索场景）。 */
  filePath?: string;
  /** 内容命中行号（内容搜索场景）。 */
  lineNumber?: number;
  /** 命中行文本（内容搜索场景）。 */
  content?: string;
  /** 该条目正文是否已被显示预算截断（web_extract 逐项截断标记）。 */
  contentTruncated?: boolean;
  /** 符号类型，如 `function` / `class` / `import`（代码图谱场景）。 */
  kind?: string;
  /** 关系边标签，如 `calls` / `called by` / `import`（代码图谱场景）。 */
  edge?: string;
}

/** diff 布局条目（单个文件的变更投影）。 */
export interface ToolDiffEntry {
  /** 变更文件路径。 */
  path: string;
  /** 文件名（path 的最后一段）。 */
  name: string;
  /** 重命名/移动后的新路径；无则为 null。 */
  newPath: string | null;
  /** 变更状态：added / modified / moved / deleted。 */
  status: string;
  /** 新增行数。 */
  insertions: number;
  /** 删除行数。 */
  deletions: number;
}

/** 结果区域的渲染投影：客户端据此渲染折叠行摘要与展开态条目。 */
export interface ToolResultProjection {
  /** 折叠行结果摘要；为 null 时客户端降级为请求摘要。 */
  summary: string | null;
  /** list 布局条目；非 list 场景为空数组。 */
  listEntries: ToolListEntry[];
  /** list 布局的空态文案；无空态时为 null。 */
  emptyLabel: string | null;
  /** diff 布局条目；非 diff 场景为空数组。 */
  diffEntries: ToolDiffEntry[];
  /** 后端剥离的索引降级/陈旧提示；无时为 null。供结果区顶部展示，避免信息丢失。 */
  notice: string | null;
}

/** 空结果投影常量，供无规则命中与非法输入复用。 */
const EMPTY_PROJECTION: ToolResultProjection = {
  summary: null,
  listEntries: [],
  emptyLabel: null,
  diffEntries: [],
  notice: null,
};

/**
 * 生成工具调用折叠态的请求摘要。
 *
 * @param toolName - 工具名，用于选择规则表条目。
 * @param args - 本次工具调用参数；缺省时按规则降级。
 * @returns 折叠行摘要文本；无规则或无可用参数时返回空串。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
export function projectToolRequestSummary(toolName: string, args?: ToolDataRecord): string {
  const rule = REQUEST_SUMMARY_RULES[toolName];
  if (!rule) {
    return "";
  }
  return rule(args ?? {});
}

/**
 * 生成工具执行结果的渲染投影。
 *
 * 失败态统一由错误信息驱动（`error` 由调用方从事件 payload 传入），成功态按工具规则
 * 从结构化数据投影摘要与条目。
 *
 * @param toolName - 工具名，用于选择规则表条目。
 * @param data - 后端透传的结构化数据（`tool_call_finished.data`）。
 * @returns 含摘要、list 条目、diff 条目的结果投影；无规则命中时返回空投影。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
export function projectToolResult(toolName: string, data?: ToolDataRecord): ToolResultProjection {
  const rule = RESULT_RULES[toolName];
  if (!rule) {
    return EMPTY_PROJECTION;
  }
  return rule(data ?? {});
}

/** 折叠态请求摘要规则表：工具名 → 参数投影函数。 */
const REQUEST_SUMMARY_RULES: Record<string, (args: ToolDataRecord) => string> = {
  read_file: (args) => {
    const path = readString(args.path);
    const offset = readNumber(args.offset);
    const limit = readNumber(args.limit);
    const start = Math.max(1, offset ?? 1);
    const endLabel = limit === null ? "End" : String(start + limit - 1);
    return `${path} L${start}-${endLabel}`;
  },
  write_file: (args) => readString(args.path) || "write",
  patch: (args) => readString(args.path) || "patch",
  delete: (args) => {
    const path = readString(args.path);
    return args.recursive === true ? `${path} （递归）` : path;
  },
  list_directory: (args) => {
    const path = readString(args.path) || ".";
    return path.trim() === "/" ? "根目录" : path;
  },
  search_files: (args) => {
    const pattern = readString(args.pattern);
    const path = readString(args.path) || ".";
    const fileGlob = readString(args.file_glob);
    const target = readString(args.target) || "content";
    const scope = path === "." ? "" : ` in ${path}`;
    // content 模式带 file_glob 时，把 glob 与搜索范围都展示出来，避免用户看不到搜索范围。
    if (target !== "files" && fileGlob) {
      return `${pattern}${scope} (${fileGlob})`;
    }
    return pattern + scope;
  },
  execute_terminal: (args) => {
    const command = readString(args.command);
    return command.trim() ? command : "（空命令）";
  },
  web_search: (args) => readString(args.query),
  web_extract: (args) => `${Array.isArray(args.urls) ? args.urls.length : 0} URL(s)`,
};

/** 执行后结果规则表：工具名 → 结构化数据投影函数。 */
const RESULT_RULES: Record<string, (data: ToolDataRecord) => ToolResultProjection> = {
  read_file: () => EMPTY_PROJECTION,
  // delete 后端显式 expandable=False，折叠行无法展开，因此若后端已透传被删路径
  // （文件分支带 display_data.path），就把它投影为折叠态摘要，避免完整路径被藏进永不展开的展开态；
  // 若后端未透传路径（链接/目录分支），返回 null 让折叠行降级回 requestSummary
  // （含 verb+路径），而非用无信息的占位符覆盖已有信息。
  delete: projectDeleteResult,
  execute_terminal: () => EMPTY_PROJECTION,
  write_file: projectFileChangeResult,
  patch: projectFileChangeResult,
  list_directory: (data) => {
    const entries = readRecordList(data.entries).map(toDirectoryEntry);
    return {
      ...EMPTY_PROJECTION,
      listEntries: entries,
      emptyLabel: entries.length === 0 ? "（空目录）" : null,
    };
  },
  search_files: (data) => {
    const searchPath = readString(data.path) || ".";
    const isFileTarget = (readString(data.target) || "content") === "files";
    const raw = Array.isArray(data.items) ? data.items : [];
    const entries = isFileTarget
      ? raw.filter((item): item is string => typeof item === "string")
          .map((item) => toFilenameEntry(item, searchPath))
      : readRecordList(raw).map((item) => toContentEntry(item, searchPath));
    return {
      ...EMPTY_PROJECTION,
      listEntries: entries,
      emptyLabel: entries.length === 0 ? "没有搜索到相关内容" : null,
    };
  },
  web_search: (data) => projectWebResult(data, "web"),
  // web_extract 后端 expand_layout="list"，展开态由 ListView 列出每个 URL 及其可读正文
  // （content 字段透传自 display_data.web[].content），修复「正文被 JSON 包裹丢弃」缺陷。
  web_extract: (data) => projectWebResult(data, "extracted"),
  // CodeGraph 只读查询：后端 result_parser 已把 vendor 文本解析为 data.codegraph.items，
  // 此处只做「结构化数据 → list 条目」的投影。explore 刻意不入表：其输出为大段带源码的
  // 半结构化文本，本期维持全文展示，走 RESULT_RULES 未命中的空投影分支。
  codegraph_search: projectCodegraphResult,
  codegraph_node: projectCodegraphResult,
  codegraph_callers: projectCodegraphResult,
  codegraph_callees: projectCodegraphResult,
  codegraph_impact: projectCodegraphResult,
};

/**
 * 把删除结果投影为折叠态摘要。
 *
 * delete 后端 `expandable=False`，折叠行无法展开，因此被删路径必须在折叠态可见。
 * 后端文件分支会透传 `display_data.path`，此处优先投影该路径；链接/目录分支未透传路径时
 * 返回 `summary: null`，让 `ToolCallCard` 降级回 `requestSummary`（含 verb+路径），
 * 绝不用无信息占位符覆盖已有信息。
 *
 * @param data - 后端透传的结构化数据（可能含 `path` / `path_basename`）。
 * @returns 含被删路径摘要的投影；无路径时返回空投影（summary 为 null）。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectDeleteResult(data: ToolDataRecord): ToolResultProjection {
  const path = readString(data.path) || readString(data.path_basename);
  if (!path) {
    return EMPTY_PROJECTION;
  }
  return { ...EMPTY_PROJECTION, summary: path };
}

/**
 * 把文件变更数据投影为 diff 条目与摘要。
 *
 * @param data - 含 `changes` 的结构化数据。
 * @returns diff 条目列表；无变更时给出空态摘要。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectFileChangeResult(data: ToolDataRecord): ToolResultProjection {
  const entries = readRecordList(data.changes).map(toDiffEntry);
  if (entries.length === 0) {
    return { ...EMPTY_PROJECTION, summary: "（没有文本变更）" };
  }
  return { ...EMPTY_PROJECTION, diffEntries: entries };
}

/**
 * 把网页搜索/提取结果投影为 list 条目与计数摘要。
 *
 * 后端 `display_data` 以 `{"web": [...]}` 携带结果数组（每项含 `title` / `url` 等），
 * 本函数把数组投影为客户端 list 条目（`title` → `name`、`url` → `path`），使展开态
 * 能直接列出结果；同时给出「N web results / N extracted pages」折叠态摘要。
 *
 * @param data - 含 `web` 数组的结构化数据。
 * @param label - 摘要文案的名词，如 `web` / `extracted`。
 * @returns 含摘要与 list 条目的结果投影；空数组时给出空态摘要。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectWebResult(data: ToolDataRecord, label: string): ToolResultProjection {
  const items = readRecordList(data.web);
  if (items.length === 0) {
    return { ...EMPTY_PROJECTION, summary: `（没有${label}结果）`, emptyLabel: "（没有结果）" };
  }
  const entries = items.map((item) => ({
    name: readString(item.title) || readString(item.url),
    path: readString(item.url),
    type: "file",
    // web_extract 的每条结果含可读正文（content），web_search 无此字段；
    // 统一带入条目，由 ListView 在 path 为 http(s) 时渲染为可折叠正文预览。
    content: readString(item.content) || undefined,
    // 透传 DisplayDataBudget 的逐项截断标记，让前端能提示「正文被截断」而非静默展示。
    contentTruncated: item.content_truncated === true,
  }));
  const noun = label === "extracted" ? "extracted pages" : "web results";
  return { ...EMPTY_PROJECTION, summary: `${items.length} ${noun}`, listEntries: entries };
}

/**
 * 把 CodeGraph 查询结果投影为 list 条目。
 *
 * 消费后端 `result_parser` 产出的 `data.codegraph`：结构化成功时读 `items`（字段已与
 * `ToolListEntry` 对齐，直接映射）；后端解析失败会降级为 `raw` 全文，此时不产出条目，
 * 由客户端沿用原有全文展示，避免「解析不了就什么都看不到」。
 *
 * @param data - 后端透传的结构化数据，预期含 `codegraph` 子字典。
 * @returns 含符号条目的结果投影；无结构化条目时返回空投影。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectCodegraphResult(data: ToolDataRecord): ToolResultProjection {
  const codegraph = readRecord(data.codegraph);
  const items = readRecordList(codegraph.items);
  if (items.length === 0) {
    // 含 raw 说明后端已降级为全文，不是「查无结果」，不应给出空态文案误导用户；
    // 但索引降级 notice 仍透传，避免提示丢失（见 projectCodegraphNoticeOnly）。
    return projectCodegraphNoticeOnly(codegraph);
  }
  const entries = items.map(toCodegraphEntry);
  const noun = items.length === 1 ? "symbol" : "symbols";
  const notice = readString(codegraph.notice) || null;
  return {
    ...EMPTY_PROJECTION,
    summary: `${items.length} ${noun}`,
    listEntries: entries,
    emptyLabel: "（没有结果）",
    notice,
  };
}

/**
 * 后端降级路径（有 notice + raw、无 items）的投影：保留索引降级提示，不产出条目。
 *
 * 与 `projectCodegraphResult` 主路径分离，避免空态分支污染「有条目」投影逻辑；
 * notice 仍透传，确保「索引可能不是最新的」这类提示不丢失。
 *
 * @param codegraph - 后端 `data.codegraph`，含 `notice` 与 `raw`、`items` 为空。
 * @returns 仅带 notice 的空投影。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function projectCodegraphNoticeOnly(codegraph: ToolDataRecord): ToolResultProjection {
  const notice = readString(codegraph.notice) || null;
  return { ...EMPTY_PROJECTION, notice };
}

/**
 * 把单条 CodeGraph 结构化条目投影为 list 条目。
 *
 * `path` 取符号所在文件路径，使条目复用 list 布局既有的「路径 + 点击打开文件」渲染；
 * `filePath` / `lineNumber` 同时保留，供客户端定位与后续跳转增强使用。
 *
 * @param item - 后端 `codegraph.items` 中的单条数据。
 * @returns list 条目。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function toCodegraphEntry(item: ToolDataRecord): ToolListEntry {
  const filePath = readString(item.filePath);
  const lineNumber = readNumber(item.lineNumber);
  return {
    name: readString(item.name),
    path: filePath,
    type: "file",
    filePath: filePath || undefined,
    lineNumber: lineNumber ?? undefined,
    kind: readString(item.kind) || undefined,
    edge: readString(item.edge) || undefined,
  };
}

/**
 * 把单条目录条目数据投影为 list 条目。
 *
 * @param entry - 后端产出的目录条目数据。
 * @returns list 条目。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function toDirectoryEntry(entry: ToolDataRecord): ToolListEntry {
  return {
    name: readString(entry.name),
    path: readString(entry.path) || ".",
    type: readString(entry.type) || "file",
  };
}

/**
 * 把文件名搜索命中投影为 list 条目。
 *
 * @param item - 搜索引擎返回的相对路径。
 * @param searchPath - 用户指定的搜索根。
 * @returns list 条目。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function toFilenameEntry(item: string, searchPath: string): ToolListEntry {
  const { name, parent } = splitPath(item);
  return {
    name,
    path: joinDisplayPath(searchPath, parent),
    type: "file",
  };
}

/**
 * 把内容搜索命中投影为 list 条目。
 *
 * @param item - 搜索引擎返回的单条命中。
 * @param searchPath - 用户指定的搜索根。
 * @returns list 条目，含命中文件路径、行号与命中行文本。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function toContentEntry(item: ToolDataRecord, searchPath: string): ToolListEntry {
  const relative = readString(item.file_path);
  const { name, parent } = splitPath(relative);
  const displayPath = joinDisplayPath(searchPath, parent);
  const lineNumber = readNumber(item.line_number) ?? 0;
  return {
    name,
    path: displayPath,
    filePath: displayPath ? `${displayPath}/${name}` : name,
    lineNumber,
    content: readString(item.content) || undefined,
  };
}

/**
 * 把单文件变更数据投影为 diff 条目。
 *
 * @param change - 后端产出的单文件变更数据。
 * @returns diff 条目。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function toDiffEntry(change: ToolDataRecord): ToolDiffEntry {
  const path = readString(change.path);
  const newPath = readString(change.new_path);
  return {
    path,
    name: splitPath(path).name,
    newPath: newPath || null,
    status: readString(change.status) || "modified",
    insertions: readNumber(change.insertions) ?? 0,
    deletions: readNumber(change.deletions) ?? 0,
  };
}

/**
 * 拆分展示用的文件名与父路径。
 *
 * @param filePath - POSIX 风格路径。
 * @returns `{ name, parent }`；无父目录时 `parent` 为空串。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function splitPath(filePath: string): { name: string; parent: string } {
  const segments = filePath.split("/").filter(Boolean);
  const name = segments.length > 0 ? segments[segments.length - 1] : filePath;
  return { name, parent: segments.slice(0, -1).join("/") };
}

/**
 * 拼接搜索根与结果相对目录，仅用于展示。
 *
 * @param searchPath - 用户指定的搜索根。
 * @param relativeParent - 命中项的相对父目录。
 * @returns 展示路径；搜索根为 `.` 或绝对路径时只保留相对父目录。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function joinDisplayPath(searchPath: string, relativeParent: string): string {
  if (!searchPath || searchPath === "." || searchPath.startsWith("/")) {
    return relativeParent;
  }
  return relativeParent ? `${searchPath}/${relativeParent}` : searchPath;
}

/**
 * 把任意值读成字符串。
 *
 * @param value - 待读取值。
 * @returns 字符串值；非字符串时返回空串。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function readString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/**
 * 把任意值读成有限数字。
 *
 * @param value - 待读取值。
 * @returns 数字值；非有限数字时返回 null。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * 把任意值读成字典数组。
 *
 * @param value - 待读取值。
 * @returns 仅含字典元素的数组；非数组时返回空数组。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function readRecordList(value: unknown): ToolDataRecord[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is ToolDataRecord => typeof item === "object" && item !== null);
}

/**
 * 把任意值读成字典。
 *
 * @param value - 待读取值。
 * @returns 字典本身；非字典（含 null 与数组）时返回空字典。
 *
 * @throws 不抛出异常。
 *
 * @sideeffect 无。
 */
function readRecord(value: unknown): ToolDataRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return {};
  }
  return value as ToolDataRecord;
}
