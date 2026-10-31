import { HostMethodError } from "../host-link/methods";
import { SUPPORTED_KEYS } from "./page-input";
import type { TabLimits } from "./tab-table";

/**
 * `browser.*` 方法的入参校验：链路只保证 `params` 是 JSON 值，这里是宿主侧唯一的校验点，
 * `BrowserHost` 直接信任这里产出的值。形状不对报 `INVALID_PARAMS`；地址协议不允许报
 * `NAVIGATION_BLOCKED`（模型可据此换地址，而不是当作调用写错）。
 */

export type NavigateTarget = { url: string } | { action: "back" | "forward" | "reload" };

export type NavigateRequest = { taskId: number; tab?: string; newTab: boolean; target: NavigateTarget; limits: TabLimits };
export type SnapshotRequest = { taskId: number; tab?: string };
export type ScreenshotRequest = SnapshotRequest & { fullPage: boolean };
export type TabsRequest = { taskId: number; action: "list" | "select" | "close"; tab?: string };

export type PageAction =
  | { type: "click"; ref: string }
  | { type: "type"; ref: string; text: string; clear: boolean; submit: boolean }
  | { type: "press"; key: string }
  | { type: "select"; ref: string; values: string[] }
  | { type: "scroll"; direction: "up" | "down"; ref?: string }
  | { type: "wait_for"; text?: string; seconds?: number };

export type ActRequest = SnapshotRequest & { actions: PageAction[] };

/** 一次调用最多几个动作：太长的序列中途页面变化的概率大，不如分几次看快照。 */
const MAX_ACTIONS = 10;
const REF = /^e\d+$/;

const TAB_ID = /^t\d+$/;
const ACTIONS = ["back", "forward", "reload"] as const;

export function parseNavigate(params: unknown): NavigateRequest {
  const value = object(params);
  const target: NavigateTarget = value.url !== undefined
    ? { url: httpUrl(value.url) }
    : { action: oneOf(value.action, ACTIONS, "action") };
  const newTab = value.new_tab === undefined ? false : boolean(value.new_tab, "new_tab");
  if (newTab && !("url" in target)) throw invalid("new_tab needs a url");
  return { taskId: taskId(value.task_id), tab: optionalTab(value.tab), newTab, target, limits: limits(value.limits) };
}

export function parseScreenshot(params: unknown): ScreenshotRequest {
  const value = object(params);
  const fullPage = value.full_page === undefined ? false : boolean(value.full_page, "full_page");
  return { ...parseSnapshot(params), fullPage };
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
  return { ...parseSnapshot(params), actions: value.actions.map(parseAction) };
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
        clear: action.clear === undefined || action.clear === null ? false : boolean(action.clear, "clear"),
        submit: action.submit === undefined || action.submit === null ? false : boolean(action.submit, "submit"),
      };
    case "press":
      return { type: "press", key: oneOf(action.key, SUPPORTED_KEYS, "key") };
    case "select":
      if (!Array.isArray(action.values) || action.values.length === 0 || !action.values.every((item) => typeof item === "string")) {
        throw invalid("select needs values");
      }
      return { type: "select", ref: ref(action.ref), values: action.values as string[] };
    case "scroll": {
      const direction = oneOf(action.direction ?? "down", ["up", "down"] as const, "direction");
      return action.ref === undefined || action.ref === null ? { type: "scroll", direction } : { type: "scroll", direction, ref: ref(action.ref) };
    }
    case "wait_for": {
      const text = action.text === undefined || action.text === null ? undefined : action.text;
      const seconds = action.seconds === undefined || action.seconds === null ? undefined : action.seconds;
      if (text !== undefined && (typeof text !== "string" || !text)) throw invalid("wait_for text must be a non-empty string");
      if (seconds !== undefined && (typeof seconds !== "number" || !(seconds > 0))) throw invalid("wait_for seconds must be positive");
      if (text === undefined && seconds === undefined) throw invalid("wait_for needs text or seconds");
      return { type: "wait_for", text: text as string | undefined, seconds: seconds as number | undefined };
    }
    default:
      throw invalid(`unknown action type: ${String(action.type)}`);
  }
}

export function parseSnapshot(params: unknown): SnapshotRequest {
  const value = object(params);
  return { taskId: taskId(value.task_id), tab: optionalTab(value.tab) };
}

export function parseDropTasks(params: unknown): number[] {
  const value = object(params);
  if (!Array.isArray(value.task_ids)) throw invalid("task_ids must be an array");
  return value.task_ids.map(taskId);
}

/** 只允许 http(s)：`file:`、`javascript:`、`chrome:` 等都不能经 Agent 打开。 */
export function httpUrl(value: unknown): string {
  if (typeof value !== "string" || value.length > 4096) throw invalid("url must be a string");
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw invalid(`not a valid absolute URL: ${value.slice(0, 200)}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
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

function optionalTab(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== "string" || !TAB_ID.test(value)) throw invalid("tab must look like t1");
  return value;
}

function limits(value: unknown): TabLimits {
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
