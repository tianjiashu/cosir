import path from "node:path";

import Ajv2020, { type ValidateFunction } from "ajv/dist/2020";

import { browserError } from "./errors";
import { isWebProtocol } from "./web-url";
import toolArgsSchemas from "./contract/tool-args.schema.json";
import type { BrowserNavigation, BrowserTabContext, BrowserTabLimits } from "../../shared/host-contract";
import type { Emulation } from "./tab-emulation";
import type { SnapshotView } from "./snapshot/views";
import type { ConsoleLevel, LogQuery } from "./tab-log";

/**
 * `browser.*` 方法的入站适配：链路只保证 `params` 是 JSON 值，这里是宿主侧唯一的校验点，产出驼峰的请求类型，
 * `BrowserHost` 直接信任它。出站的线上形状在 `wire.ts`。
 *
 * 工具参数的契约只有一份：后端的 Pydantic 参数模型（也是给模型的 schema），按线上实际形状导出成
 * `contract/tool-args.schema.json`——后端照 `model_dump()` 原样发送，每个字段都在，未填的可选字段为 null（后端守卫
 * 测试保证导出文件是最新的，见 `tests/test_browser_contract_guard.py`）。这里按它校验形状、枚举与上限，不再手写第二
 * 份，也不补缺省值；null 只在映射成请求类型时转成「未给」。跨字段规则（url 与 action 二选一等）只在后端，宿主信任
 * 后端已经校验过。手写的只有宿主自己的键（Task、上限、下载目录）与语义检查（地址协议报 `NAVIGATION_BLOCKED`，
 * 模型可据此换地址，而不是当作调用写错）。
 */

/** 后端每个请求都带、不属于工具参数的键。 */
const HOST_KEYS = new Set(["task_id", "limits", "download_dir"]);

type ToolName = keyof typeof toolArgsSchemas;
const ajv = new Ajv2020({ allErrors: false, strict: false });
const validators = new Map<ToolName, ValidateFunction>(
  (Object.keys(toolArgsSchemas) as ToolName[]).map((name) => [name, ajv.compile(toolArgsSchemas[name])]),
);

/**
 * 每个请求都带后端给出的标签上下文：宿主据此刷新上限（页面开出的标签没有请求可带，用最近一次的），下载目录
 * 只在这次请求新建标签时用。
 */
type TaskRequest = { taskId: number; context: BrowserTabContext };
/** 指向本 Task 的某个标签（省略即当前标签）。 */
export type TabRequest = TaskRequest & { tab?: string };
export type NavigateRequest = TabRequest & {
  newTab: boolean;
  target: BrowserNavigation;
  /** 只含要改的项；`colorScheme: null` 表示取消模拟、跟随应用外观。 */
  emulation?: Partial<Emulation>;
};
export type SnapshotRequest = TabRequest & { view: SnapshotView };
/** 截哪里：视口、整页（`fullPage`），或单个元素（`ref`，与 `fullPage` 互斥，后端校验过）。 */
export type ScreenshotRequest = TabRequest & { fullPage: boolean; ref?: string };
export type TabsRequest = TabRequest & { action: "list" | "select" | "close" };
export type LogsRequest = TabRequest & { query: LogQuery };
export type EvaluateRequest = TabRequest & { expression: string };

/** `ControlOrMeta` 在 macOS 上是 Meta、其他平台是 Control（与 Playwright 同名约定），模型不必知道宿主平台。 */
export type Modifier = "Alt" | "Control" | "Meta" | "Shift" | "ControlOrMeta";

/** 鼠标落点：快照里的元素（取中心），或视口坐标（CSS 像素，与视口截图的像素一致）。 */
export type PointerTarget = { ref: string } | { x: number; y: number };

export type PageAction =
  | { type: "click"; target: PointerTarget; button: "left" | "right"; clickCount: 1 | 2; modifiers: Modifier[] }
  | { type: "hover"; target: PointerTarget }
  | { type: "drag"; start: PointerTarget; end: PointerTarget }
  | { type: "type"; ref: string; text: string; clear: boolean; submit: boolean; keyByKey: boolean }
  | { type: "press"; key: string; modifiers: Modifier[]; ref?: string }
  | { type: "select"; ref: string; values: string[] }
  /** 有方向：滚动整页，或（带 ref）滚动包含该元素的可滚动区域；只有 ref：把元素滚入视野。 */
  | { type: "scroll"; direction?: "up" | "down"; ref?: string }
  /** `text` 与 `textGone` 至多一个；三者至少一个。 */
  | { type: "wait_for"; text?: string; textGone?: string; seconds?: number }
  /** 路径是后端解析好的本机绝对路径；省略 ref 即回答 Agent 的动作让页面打开的选文件框。 */
  | { type: "upload"; paths: string[]; ref?: string }
  /** 回答待答的 JS 弹窗：指明 id，或省略而回答本批前面的动作弹出的那个；`text` 只对 prompt 有意义。 */
  | { type: "dialog"; accept: boolean; text?: string; id?: number };

export type ActRequest = TabRequest & { actions: PageAction[] };

export function parseNavigate(params: unknown): NavigateRequest {
  const { request, args } = toolRequest("browser_navigate", params);
  const target: BrowserNavigation = args.url !== null
    ? { url: httpUrl(args.url) }
    : { action: args.action as "back" | "forward" | "reload" };
  const viewport = args.viewport as { width: number; height: number; mobile: boolean } | null;
  const scheme = args.color_scheme as "light" | "dark" | "auto" | null;
  const emulation: Partial<Emulation> = {
    ...viewport,
    ...(scheme === null ? {} : { colorScheme: scheme === "auto" ? null : scheme }),
  };
  return { ...request, newTab: args.new_tab as boolean, target, ...(Object.keys(emulation).length ? { emulation } : {}) };
}

export function parseSnapshot(params: unknown): SnapshotRequest {
  const { request, args } = toolRequest("browser_snapshot", params);
  const view: SnapshotView = { scope: args.scope as "page" | "viewport", fromLine: (args.from_line as number | null) ?? 1 };
  const find = given(args.find as string | null);
  return { ...request, view: find === undefined ? view : { ...view, find } };
}

export function parseScreenshot(params: unknown): ScreenshotRequest {
  const { request, args } = toolRequest("browser_screenshot", params);
  const ref = given(args.ref as string | null);
  return { ...request, fullPage: args.full_page as boolean, ...(ref ? { ref } : {}) };
}

export function parseTabs(params: unknown): TabsRequest {
  const { request, args } = toolRequest("browser_tabs", params);
  return { ...request, action: args.action as TabsRequest["action"] };
}

export function parseAct(params: unknown): ActRequest {
  const { request, args } = toolRequest("browser_act", params);
  return { ...request, actions: (args.actions as WireAction[]).map(toAction) };
}

export function parseLogs(params: unknown): LogsRequest {
  const { request, args } = toolRequest("browser_logs", params);
  const common = { since: given(args.since as number | null), pattern: given(args.pattern as string | null), limit: args.limit as number };
  const query: LogQuery = args.kind === "console"
    ? { kind: "console", ...common, level: given(args.level as ConsoleLevel | null) }
    : { kind: "network", ...common, failedOnly: args.failed_only as boolean };
  return { ...request, query };
}

export function parseEvaluate(params: unknown): EvaluateRequest {
  const { request, args } = toolRequest("browser_evaluate", params);
  return { ...request, expression: args.expression as string };
}

export function parseDropTasks(params: unknown): number[] {
  const value = object(params);
  if (!Array.isArray(value.task_ids)) throw invalid("task_ids must be an array");
  return value.task_ids.map(taskId);
}

/** 要打开的地址（长度等形状由调用方的边界校验过）：先要求是合法的绝对地址（INVALID_PARAMS），再按 `isWebProtocol` 判定（NAVIGATION_BLOCKED）。 */
export function httpUrl(value: unknown): string {
  let url: URL;
  try {
    url = new URL(String(value));
  } catch {
    throw invalid(`not a valid absolute URL: ${String(value).slice(0, 200)}`);
  }
  if (!isWebProtocol(url.protocol)) {
    throw browserError("NAVIGATION_BLOCKED", `only http and https pages can be opened (got ${url.protocol})`);
  }
  return url.href;
}

/** 拆出宿主自己的键并校验，其余按工具的 schema 校验（每个字段都在，可选字段可能为 null）。 */
function toolRequest(tool: ToolName, params: unknown): { request: TabRequest; args: Record<string, unknown> } {
  const value = object(params);
  const args = Object.fromEntries(Object.entries(value).filter(([key]) => !HOST_KEYS.has(key)));
  const validate = validators.get(tool)!;
  if (!validate(args)) throw invalid(ajv.errorsText(validate.errors, { dataVar: "params" }).slice(0, 500));
  const tab = given(args.tab as string | null);
  return { request: { taskId: taskId(value.task_id), context: tabContext(value), ...(tab ? { tab } : {}) }, args };
}

/** 线上的鼠标落点：ref 与坐标恰好给了一种（后端校验过），另一种为 null。 */
type WirePoint = { ref?: string | null; x?: number | null; y?: number | null };

/** 线上的动作：蛇形字段名，每个字段都在、未填的为 null（按 schema 校验过）。 */
type WireAction = WirePoint & {
  type: PageAction["type"];
  button?: "left" | "right";
  click_count?: 1 | 2;
  start?: WirePoint;
  end?: WirePoint;
  text?: string | null;
  clear?: boolean;
  submit?: boolean;
  key_by_key?: boolean;
  key?: string;
  modifiers?: Modifier[] | null;
  values?: string[];
  paths?: string[];
  direction?: "up" | "down" | null;
  text_gone?: string | null;
  seconds?: number | null;
  accept?: boolean;
  id?: number | null;
};

/** 线上动作 → 执行器用的形状：改字段名、null 转成「未给」。 */
function toAction(action: WireAction): PageAction {
  switch (action.type) {
    case "click":
      return { type: "click", target: pointer(action), button: action.button!, clickCount: action.click_count!, modifiers: action.modifiers ?? [] };
    case "hover":
      return { type: "hover", target: pointer(action) };
    case "drag":
      return { type: "drag", start: pointer(action.start!), end: pointer(action.end!) };
    case "type":
      return { type: "type", ref: action.ref!, text: action.text!, clear: action.clear!, submit: action.submit!, keyByKey: action.key_by_key! };
    case "press":
      return { type: "press", key: action.key!, modifiers: action.modifiers ?? [], ...(action.ref ? { ref: action.ref } : {}) };
    case "select":
      return { type: "select", ref: action.ref!, values: action.values! };
    case "scroll":
      return { type: "scroll", direction: given(action.direction), ref: given(action.ref) };
    case "wait_for":
      return { type: "wait_for", text: given(action.text), textGone: given(action.text_gone), seconds: given(action.seconds) };
    case "dialog":
      return { type: "dialog", accept: action.accept!, text: given(action.text), id: given(action.id) };
    case "upload":
      return { type: "upload", paths: action.paths!.map(absolutePath), ...(action.ref ? { ref: action.ref } : {}) };
  }
}

function pointer(point: WirePoint): PointerTarget {
  return point.ref ? { ref: point.ref } : { x: point.x!, y: point.y! };
}

/** 本机文件路径由后端解析成绝对路径交来；相对路径会按宿主进程的工作目录解析成别的文件。 */
function absolutePath(value: string): string {
  if (!path.isAbsolute(value)) throw invalid(`upload paths must be absolute: ${value.slice(0, 200)}`);
  return value;
}

/** 契约里 null 表示「未给」。 */
function given<T>(value: T | null | undefined): T | undefined {
  return value ?? undefined;
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw invalid("params must be an object");
  return value as Record<string, unknown>;
}

function taskId(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) throw invalid("task_id must be a positive integer");
  return value as number;
}

/** 后端随请求带来的标签上下文：上限与 Task 所在工作区的下载目录（本机绝对路径）。 */
function tabContext(value: Record<string, unknown>): BrowserTabContext {
  const downloadDir = value.download_dir;
  if (typeof downloadDir !== "string" || !path.isAbsolute(downloadDir)) throw invalid("download_dir must be an absolute path");
  return { limits: limits(value.limits), downloadDir };
}

function limits(value: unknown): BrowserTabLimits {
  const { per_task: perTask, total } = object(value);
  if (!positive(perTask) || !positive(total)) throw invalid("limits must carry positive per_task and total");
  return { perTask, total };
}

function positive(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0;
}

function invalid(message: string) {
  return browserError("INVALID_PARAMS", message);
}
