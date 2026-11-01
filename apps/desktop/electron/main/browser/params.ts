import { HostMethodError } from "../host-link/methods";
import { isSupportedKey, MODIFIERS, type Modifier } from "./page-input";
import { BROWSER_TAB_ID } from "../../shared/host-contract";
import { isWebProtocol } from "./web-url";
import { BROWSER_NAVIGATION_ACTIONS, type BrowserNavigation, type BrowserTabLimits } from "../../shared/host-contract";

/**
 * `browser.*` 方法的入参校验：链路只保证 `params` 是 JSON 值，这里是宿主侧唯一的校验点，
 * `BrowserHost` 直接信任这里产出的值。形状不对报 `INVALID_PARAMS`；地址协议不允许报
 * `NAVIGATION_BLOCKED`（模型可据此换地址，而不是当作调用写错）。
 */

export type NavigateRequest = { taskId: number; tab?: string; newTab: boolean; target: BrowserNavigation; limits: BrowserTabLimits };
/** 指向本 Task 的某个标签（省略即当前标签）。 */
type TabRequest = { taskId: number; tab?: string };
export type SnapshotRequest = TabRequest & { fromLine: number };
export type ScreenshotRequest = TabRequest & { fullPage: boolean };
export type TabsRequest = { taskId: number; action: "list" | "select" | "close"; tab?: string };

export type PageAction =
  | { type: "click"; ref: string }
  | { type: "type"; ref: string; text: string; clear: boolean; submit: boolean }
  | { type: "hover"; ref: string }
  | { type: "press"; key: string; modifiers: Modifier[]; ref?: string }
  | { type: "select"; ref: string; values: string[] }
  /** 有方向：滚动整页，或（带 ref）滚动包含该元素的可滚动区域；只有 ref：把元素滚入视野。 */
  | { type: "scroll"; direction?: "up" | "down"; ref?: string }
  /** `text` 与 `textGone` 至多一个；三者至少一个。 */
  | { type: "wait_for"; text?: string; textGone?: string; seconds?: number };

export type ActRequest = TabRequest & { actions: PageAction[] };

/** 一次调用最多几个动作：太长的序列中途页面变化的概率大，不如分几次看快照。 */
const MAX_ACTIONS = 10;
const REF = /^e\d+$/;

export function parseNavigate(params: unknown): NavigateRequest {
  const value = object(params);
  const target: BrowserNavigation = value.url !== undefined
    ? { url: httpUrl(value.url) }
    : { action: oneOf(value.action, BROWSER_NAVIGATION_ACTIONS, "action") };
  const newTab = value.new_tab === undefined ? false : boolean(value.new_tab, "new_tab");
  if (newTab && !("url" in target)) throw invalid("new_tab needs a url");
  return { taskId: taskId(value.task_id), tab: optionalTab(value.tab), newTab, target, limits: limits(value.limits) };
}

export function parseScreenshot(params: unknown): ScreenshotRequest {
  const value = object(params);
  const fullPage = value.full_page === undefined ? false : boolean(value.full_page, "full_page");
  return { ...parseTab(value), fullPage };
}

export function parseTabs(params: unknown): TabsRequest {
  const value = object(params);
  const action = oneOf(value.action, ["list", "select", "close"] as const, "action");
  const tab = optionalTab(value.tab);
  if (action !== "list" && tab === undefined) throw invalid(`${action} needs a tab`);
  return { taskId: taskId(value.task_id), action, tab };
}

export function parseAct(params: unknown): ActRequest {
  const value = object(params);
  if (!Array.isArray(value.actions) || value.actions.length === 0 || value.actions.length > MAX_ACTIONS) {
    throw invalid(`actions must be a list of 1 to ${MAX_ACTIONS} actions`);
  }
  return { ...parseTab(value), actions: value.actions.map(parseAction) };
}

function parseAction(item: unknown): PageAction {
  const action = object(item);
  switch (action.type) {
    case "click":
      return { type: "click", ref: ref(action.ref) };
    case "type":
      if (typeof action.text !== "string" || action.text.length > 10_000) throw invalid("type needs text");
      return {
        type: "type",
        ref: ref(action.ref),
        text: action.text,
        clear: absent(action.clear) ? false : boolean(action.clear, "clear"),
        submit: absent(action.submit) ? false : boolean(action.submit, "submit"),
      };
    case "hover":
      return { type: "hover", ref: ref(action.ref) };
    case "press": {
      if (typeof action.key !== "string" || !isSupportedKey(action.key)) throw invalid("press needs a supported key");
      const modifiers = absent(action.modifiers) ? [] : action.modifiers;
      if (!Array.isArray(modifiers)) throw invalid("modifiers must be a list");
      return {
        type: "press",
        key: action.key,
        modifiers: modifiers.map((item) => oneOf(item, MODIFIERS, "modifier")),
        ...(absent(action.ref) ? {} : { ref: ref(action.ref) }),
      };
    }
    case "select":
      if (!Array.isArray(action.values) || action.values.length === 0 || !action.values.every((item) => typeof item === "string")) {
        throw invalid("select needs values");
      }
      return { type: "select", ref: ref(action.ref), values: action.values as string[] };
    case "scroll": {
      if (absent(action.direction) && absent(action.ref)) throw invalid("scroll needs a direction, a ref, or both");
      return {
        type: "scroll",
        ...(absent(action.direction) ? {} : { direction: oneOf(action.direction, ["up", "down"] as const, "direction") }),
        ...(absent(action.ref) ? {} : { ref: ref(action.ref) }),
      };
    }
    case "wait_for": {
      const text = optionalText(action.text, "text");
      const textGone = optionalText(action.text_gone, "text_gone");
      const seconds = absent(action.seconds) ? undefined : action.seconds;
      if (seconds !== undefined && (typeof seconds !== "number" || !(seconds > 0))) throw invalid("wait_for seconds must be positive");
      if (text !== undefined && textGone !== undefined) throw invalid("wait_for takes text or text_gone, not both");
      if (text === undefined && textGone === undefined && seconds === undefined) throw invalid("wait_for needs text, text_gone or seconds");
      return { type: "wait_for", text, textGone, seconds: seconds as number | undefined };
    }
    default:
      throw invalid(`unknown action type: ${String(action.type)}`);
  }
}

export function parseSnapshot(params: unknown): SnapshotRequest {
  const value = object(params);
  const fromLine = absent(value.from_line) ? 1 : value.from_line;
  if (!positive(fromLine)) throw invalid("from_line must be a positive integer");
  return { ...parseTab(value), fromLine };
}

function parseTab(value: Record<string, unknown>): TabRequest {
  return { taskId: taskId(value.task_id), tab: optionalTab(value.tab) };
}

export function parseDropTasks(params: unknown): number[] {
  const value = object(params);
  if (!Array.isArray(value.task_ids)) throw invalid("task_ids must be an array");
  return value.task_ids.map(taskId);
}

/** Agent 打开的地址：先要求是合法的绝对地址（INVALID_PARAMS），再按 `isWebProtocol` 判定（NAVIGATION_BLOCKED）。 */
export function httpUrl(value: unknown): string {
  if (typeof value !== "string" || value.length > 4096) throw invalid("url must be a string");
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw invalid(`not a valid absolute URL: ${value.slice(0, 200)}`);
  }
  if (!isWebProtocol(url.protocol)) {
    throw new HostMethodError("NAVIGATION_BLOCKED", `only http and https pages can be opened (got ${url.protocol})`);
  }
  return url.href;
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw invalid("params must be an object");
  return value as Record<string, unknown>;
}

function taskId(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) <= 0) throw invalid("task_id must be a positive integer");
  return value as number;
}

function ref(value: unknown): string {
  if (typeof value !== "string" || !REF.test(value)) throw invalid("ref must look like e12, from the latest snapshot");
  return value;
}

function boolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw invalid(`${field} must be true or false`);
  return value;
}

/** 后端不发未填的可选字段；`null` 也按缺省理解，宿主测试与链路之外的调用方可能带上。 */
function absent(value: unknown): value is undefined | null {
  return value === undefined || value === null;
}

function optionalText(value: unknown, field: string): string | undefined {
  if (absent(value)) return undefined;
  if (typeof value !== "string" || !value) throw invalid(`wait_for ${field} must be a non-empty string`);
  return value;
}

function optionalTab(value: unknown): string | undefined {
  if (absent(value)) return undefined;
  if (typeof value !== "string" || !BROWSER_TAB_ID.test(value)) throw invalid("tab must look like t1");
  return value;
}

function limits(value: unknown): BrowserTabLimits {
  const { per_task: perTask, total } = object(value);
  if (!positive(perTask) || !positive(total)) throw invalid("limits must carry positive per_task and total");
  return { perTask, total };
}

function positive(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0;
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], field: string): T {
  if (!(allowed as readonly unknown[]).includes(value)) throw invalid(`${field} must be one of ${allowed.join(", ")}`);
  return value as T;
}

function invalid(message: string): HostMethodError {
  return new HostMethodError("INVALID_PARAMS", message);
}
