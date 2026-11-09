/**
 * browser_act 动作结算矩阵：全部动作的失败路径按设计语义结算（failed=确定未生效，unknown=可能已生效）。
 * 覆盖本次 P1-1 的 8 条误判路径，同时固定其余正向/保守路径（如按下后失败、insertText、select no-option）。
 *
 * 设计依据：docs/plan/browser-reliability-refactor-plan.md:118-130；
 * 实现：input/actions.ts、operation.ts（effect/NoEffectError）。
 */
import { describe, expect, it, vi } from "vitest";
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));

import { runActions, type ActResult } from "@/electron/main/browser/input/actions";
import type { PageAction } from "@/electron/main/browser/params";
import type { BrowserDialog } from "@/electron/shared/host-contract";
import { HostMethodError } from "@/electron/main/host-link/method-registry";
import { makeSession, type FixtureConfig } from "./browser-action-settlement-fixture";

type Expectation = "failed" | "unknown" | "done";

type Case = {
  group: string;
  name: string;
  action: PageAction;
  config?: FixtureConfig;
  expected: Expectation;
  /** 期望的中断码（可选，用来固定错误分类）。 */
  code?: string;
  /** 复核注记：这条路径的副作用判定。 */
  note: string;
  abortDuring?: number;
  abortBefore?: boolean;
};

const click = (target: { ref: string } | { x: number; y: number }): PageAction =>
  ({ type: "click", target, button: "left", clickCount: 1, modifiers: [] });
const hover = (target: { ref: string } | { x: number; y: number }): PageAction => ({ type: "hover", target });
const drag = (start: { ref: string } | { x: number; y: number }, end: { ref: string } | { x: number; y: number }): PageAction =>
  ({ type: "drag", start, end });
const type = (ref: string, extra: Partial<Extract<PageAction, { type: "type" }>> = {}): PageAction =>
  ({ type: "type", ref, text: "x", clear: false, submit: false, keyByKey: false, ...extra });
const press = (key: string, ref?: string): PageAction => ({ type: "press", key, modifiers: [], ...(ref ? { ref } : {}) });
const select = (ref: string, values: string[]): PageAction => ({ type: "select", ref, values });
const scroll = (direction: "up" | "down" | undefined, ref?: string): PageAction => ({ type: "scroll", direction, ...(ref ? { ref } : {}) });
const waitFor = (target: { text?: string; textGone?: string; seconds?: number }): PageAction => ({ type: "wait_for", ...target });
const upload = (paths: string[], ref?: string): PageAction => ({ type: "upload", paths, ...(ref ? { ref } : {}) });
const dialogAction = (extra: Partial<Extract<PageAction, { type: "dialog" }>> = {}): PageAction => ({ type: "dialog", accept: true, ...extra });

const detached = { isConnected: "detached" };
const disabled = { "the element is disabled": "the element is disabled" };
const notEditable = { "the element is not editable": "the element is not editable" };
const hidden = { "the element is hidden": "the element is hidden" };
const scrollEnd = { "is already at the": "this area is already at the bottom" };
const notSelect = { "is not a select list": "the element is not a select list" };
const noOption = { "no option matched": 'no option matched ["zzz"]' };

const dialog: BrowserDialog = { id: 7, type: "confirm", message: "delete?", defaultText: "" };

const cases: Case[] = [
  // ───────────────────────── click ─────────────────────────
  { group: "click", name: "ref 从未登记（resolveRef 直接拒绝）", action: click({ ref: "e404" }), expected: "failed", code: "REF_STALE", note: "确定无副作用（未派发任何命令）" },
  { group: "click", name: "ref 已脱离文档", action: click({ ref: "e9" }), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "只读校验在 effect 之外，正确" },
  { group: "click", name: "元素 disabled", action: click({ ref: "e9" }), config: { answers: disabled }, expected: "failed", code: "NOT_INTERACTABLE", note: "输入前检查，无副作用" },
  { group: "click", name: "元素被遮挡", action: click({ ref: "e9" }), config: { answers: { "the element is covered at its input point": "the element is covered at its input point" } }, expected: "failed", code: "NOT_INTERACTABLE", note: "输入前检查，无副作用" },
  { group: "click", name: "元素无布局（getBoxModel 报错）", action: click({ ref: "e9" }), config: { failAfterDispatch: (method) => (method === "DOM.getBoxModel" ? new Error("Could not compute box model.") : undefined) }, expected: "failed", code: "NOT_INTERACTABLE", note: "只读几何失败，无副作用" },
  { group: "click", name: "坐标超出视口", action: click({ x: 500, y: 10 }), expected: "failed", code: "OUT_OF_VIEWPORT", note: "未派发输入" },
  { group: "click", name: "动作前页面已停住（弹窗）", action: click({ ref: "e9" }), config: { pausedBy: new HostMethodError("DIALOG_OPEN", "paused") }, expected: "failed", code: "DIALOG_OPEN", note: "steps 循环入口的暂停检查，未派发" },
  { group: "click", name: "mousePressed 已派发后失败", action: click({ ref: "e9" }), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchMouseEvent" && p.type === "mousePressed" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "按下可能已生效 → unknown 正确" },
  { group: "click", name: "mousePressed 派发前被闸门拒绝", action: click({ ref: "e9" }), config: { rejectBeforeDispatch: (m, p) => (m === "Input.dispatchMouseEvent" && p.type === "mousePressed" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "命令未派发 → failed 正确" },
  { group: "click", name: "mouseReleased 已派发后失败", action: click({ ref: "e9" }), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchMouseEvent" && p.type === "mouseReleased" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "按下已派发，点击可能已生效" },
  { group: "click", name: "预置 mouseMoved 已派发后失败", action: click({ ref: "e9" }), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchMouseEvent" && p.type === "mouseMoved" ? new Error("boom") : undefined) }, expected: "failed", code: "ACTION_FAILED", note: "鼠标移动是视图副作用（hover 态），点击未开始；failed 可接受，但 mover 命令未纳入 effect" },

  // ───────────────────────── hover ─────────────────────────
  { group: "hover", name: "ref 已脱离文档", action: hover({ ref: "e9" }), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "定位在 effect 之外" },
  { group: "hover", name: "元素被遮挡", action: hover({ ref: "e9" }), config: { answers: { "the element is covered at its input point": "the element is covered at its input point" } }, expected: "failed", code: "NOT_INTERACTABLE", note: "无副作用" },
  { group: "hover", name: "坐标超出视口", action: hover({ x: 500, y: 10 }), expected: "failed", code: "OUT_OF_VIEWPORT", note: "无副作用" },
  { group: "hover", name: "mouseMoved 已派发后失败", action: hover({ ref: "e9" }), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchMouseEvent" && p.type === "mouseMoved" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "hover 本身可能已生效 → unknown 正确" },
  { group: "hover", name: "mouseMoved 派发前被闸门拒绝", action: hover({ ref: "e9" }), config: { rejectBeforeDispatch: (m) => (m === "Input.dispatchMouseEvent" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "未派发 → failed 正确" },

  // ───────────────────────── drag ─────────────────────────
  { group: "drag", name: "起点 ref 已脱离文档", action: drag({ ref: "e9" }, { x: 30, y: 20 }), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "两端定位在 effect 之外" },
  { group: "drag", name: "终点 ref 已脱离文档", action: drag({ x: 5, y: 5 }, { ref: "e9" }), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "未派发拖动输入" },
  { group: "drag", name: "终点超出视口", action: drag({ x: 5, y: 5 }, { x: 500, y: 10 }), expected: "failed", code: "OUT_OF_VIEWPORT", note: "未派发拖动输入" },
  { group: "drag", name: "拖放拦截开启命令已派发后失败", action: drag({ x: 5, y: 5 }, { x: 30, y: 20 }), config: { failAfterDispatch: (m, p) => (m === "Input.setInterceptDrags" && p.enabled === true ? new Error("boom") : undefined) }, expected: "failed", code: "ACTION_FAILED", note: "拖动尚未开始；拦截状态可能残留（既有 P2 问题），不改变步骤语义" },
  { group: "drag", name: "mousePressed 已派发后失败", action: drag({ x: 5, y: 5 }, { x: 30, y: 20 }), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchMouseEvent" && p.type === "mousePressed" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "按下已派发 → unknown 正确" },
  { group: "drag", name: "中途 mouseMoved 已派发后失败", action: drag({ x: 5, y: 5 }, { x: 30, y: 20 }), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchMouseEvent" && p.type === "mouseMoved" && p.buttons === 1 ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "拖动可能已开始 → unknown 正确" },
  { group: "drag", name: "drop 派发后页面弹窗", action: drag({ x: 5, y: 5 }, { x: 30, y: 20 }), config: { dragData: { items: [], dragOperationsMask: 1 }, dialogOnDispatch: (m, p) => m === "Input.dispatchDragEvent" && p.type === "drop" }, expected: "unknown", code: "DIALOG_OPEN", note: "drop 已投递，可能已生效 → unknown 正确" },
  { group: "drag", name: "收尾撤销拖放拦截命令已派发后失败", action: drag({ x: 5, y: 5 }, { x: 30, y: 20 }), config: { failAfterDispatch: (m, p) => (m === "Input.setInterceptDrags" && p.enabled === false ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "拖动本身已完成（unsubscribe 后 finally 再抛）；unknown 偏保守但不误导重放" },
  { group: "drag", name: "mousePressed 派发前被闸门拒绝", action: drag({ x: 5, y: 5 }, { x: 30, y: 20 }), config: { rejectBeforeDispatch: (m, p) => (m === "Input.dispatchMouseEvent" && p.type === "mousePressed" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "未派发拖动输入 → failed 正确" },

  // ───────────────────────── type ─────────────────────────
  { group: "type", name: "ref 从未登记", action: type("e404"), expected: "failed", code: "REF_STALE", note: "对照：element() 在 effect 之外" },
  { group: "type", name: "ref 已脱离文档", action: type("e9"), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "对照：同一 REF_STALE 在 type 是 failed" },
  { group: "type", name: "元素不可编辑", action: type("e9"), config: { answers: notEditable }, expected: "failed", code: "NOT_INTERACTABLE", note: "输入前检查" },
  { group: "type", name: "元素不可见", action: type("e9"), config: { answers: hidden }, expected: "failed", code: "NOT_INTERACTABLE", note: "输入前检查" },
  { group: "type", name: "focus 命令已派发后失败", action: type("e9"), config: { failAfterDispatch: (m) => (m === "DOM.focus" ? new Error("boom") : undefined) }, expected: "failed", code: "ACTION_FAILED", note: "尚未写入任何字符；focus 可能触发页面 onfocus（视图/页面小副作用未计入）" },
  { group: "type", name: "clear 的页面函数已派发后失败", action: type("e9", { clear: true }), config: { failAfterDispatch: (m, p) => (m === "Runtime.callFunctionOn" && String(p.functionDeclaration).includes('this.value = ""') ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "清空可能已发生 → unknown 正确" },
  { group: "type", name: "insertText 已派发后失败", action: type("e9"), config: { failAfterDispatch: (m) => (m === "Input.insertText" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "文字可能已写入 → unknown 正确" },
  { group: "type", name: "逐键输入第 2 个字符 keyDown 已派发后失败", action: type("e9", { text: "ab", keyByKey: true }), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchKeyEvent" && p.type === "keyDown" && p.key === "b" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "已输入 a → unknown 正确" },
  { group: "type", name: "submit 的 Enter keyDown 已派发后失败", action: type("e9", { text: "a", submit: true }), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchKeyEvent" && p.type === "keyDown" && p.key === "Enter" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "已输入 a，提交可能发生 → unknown 正确" },
  { group: "type", name: "insertText 派发前被闸门拒绝", action: type("e9"), config: { rejectBeforeDispatch: (m) => (m === "Input.insertText" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "未派发文字 → failed 正确" },

  // ───────────────────────── press ─────────────────────────
  { group: "press", name: "带 ref 且已脱离文档", action: press("a", "e9"), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "定位在 effect 之外" },
  { group: "press", name: "keyDown 派发前被闸门拒绝", action: press("a"), config: { rejectBeforeDispatch: (m) => (m === "Input.dispatchKeyEvent" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "未派发按键 → failed 正确" },
  { group: "press", name: "keyUp 已派发后失败", action: press("a"), config: { failAfterDispatch: (m, p) => (m === "Input.dispatchKeyEvent" && p.type === "keyUp" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "keyDown 已派发 → unknown 正确（收尾会置 unsafe）" },

  // ───────────────────────── select ─────────────────────────
  { group: "select", name: "ref 从未登记", action: select("e404", ["a"]), expected: "failed", code: "REF_STALE", note: "无副作用" },
  { group: "select", name: "ref 已脱离文档", action: select("e9", ["a"]), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "element() 在 effect 之外 → 对照正确" },
  { group: "select", name: "元素 disabled（actionable 失败）", action: select("e9", ["a"]), config: { answers: disabled }, expected: "failed", code: "NOT_INTERACTABLE", note: "actionable 在 effect 之外 → failed 正确；不是误判" },
  { group: "select", name: "元素被遮挡（actionable 失败）", action: select("e9", ["a"]), config: { answers: { "the element is covered at its input point": "the element is covered at its input point" } }, expected: "failed", code: "NOT_INTERACTABLE", note: "actionable 在 effect 之外 → failed 正确" },
  { group: "select", name: "目标不是 <select>（页面函数在任何修改前返回）", action: select("e9", ["b"]), config: { answers: notSelect }, expected: "failed", code: "ACTION_FAILED", note: "RED：真实 Chromium 探针确认无 mutation；应 failed" },
  { group: "select", name: "没有 option 匹配（页面函数已改过 selected）", action: select("e9", ["zzz"]), config: { answers: noOption }, expected: "unknown", code: "ACTION_FAILED", note: "真实 Chromium 探针：单选从 b 变 a、多选清空 → 有副作用，unknown 正确" },
  { group: "select", name: "页面函数入口节点已消失（actionable 后竞态）", action: select("e9", ["a"]), config: { failAfterDispatch: (m, _p, counts) => (m === "DOM.resolveNode" && counts["DOM.resolveNode"] === 3 ? new Error("No node found for given backend id") : undefined) }, expected: "failed", code: "REF_STALE", note: "RED：第 4 类；effect 内只有只读 resolveNode，无 mutation；出现需节点在检查后消失（低概率竞态）" },
  { group: "select", name: "页面函数派发前被闸门拒绝（入口 resolveNode 已派发）", action: select("e9", ["a"]), config: { rejectBeforeDispatch: (m, _p, counts) => (m === "Runtime.callFunctionOn" && counts["Runtime.callFunctionOn"] === 2 ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "RED（同根因）：第 3 次 callFunctionOn（SELECT_OPTIONS）派发前被闸门拒绝（前两次是只读检查），effect 内只派发过只读 resolveNode，确定无副作用，应 failed" },

  // ───────────────────────── scroll ─────────────────────────
  { group: "scroll", name: "带方向 + ref 从未登记", action: scroll("down", "e404"), expected: "failed", code: "REF_STALE", note: "resolveRef 在任何命令前抛 → failed 正确（REF_STALE 不全是误判）" },
  { group: "scroll", name: "带方向 + ref 已脱离文档（只读校验被包进 effect）", action: scroll("down", "e9"), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "RED（初判 b）：生产 page-functions.ts:59 把 element() 包进 effect" },
  { group: "scroll", name: "带方向 + ref，SCROLL_END（确定没滚动）", action: scroll("down", "e9"), config: { answers: scrollEnd }, expected: "failed", code: "SCROLL_END", note: "RED（初判 a-带 ref）：真实 Chromium 探针确认 scrollTop 未变" },
  { group: "scroll", name: "不带 ref，SCROLL_END（整页确定没滚动）", action: scroll("down"), config: { answers: scrollEnd }, expected: "failed", code: "SCROLL_END", note: "RED（初判 a-不带 ref）：同上" },
  { group: "scroll", name: "带方向 + ref，SCROLL_AREA 的 resolveNode 已派发后失败", action: scroll("down", "e9"), config: { failAfterDispatch: (m, _p, counts) => (m === "DOM.resolveNode" && counts["DOM.resolveNode"] === 2 ? new Error("No node found for given backend id") : undefined) }, expected: "failed", code: "REF_STALE", note: "RED：初判 b 的第二个来源（节点在 element() 校验后消失）" },
  { group: "scroll", name: "不带 ref，页面函数抛错（可能已滚过）", action: scroll("down"), config: { answers: { "is already at the": new Error("page broke") } }, expected: "unknown", code: "ACTION_FAILED", note: "无法判定抛错发生在 scrollBy 前后 → unknown 合理" },
  { group: "scroll", name: "带方向 + ref，页面函数派发前被闸门拒绝（入口 resolveNode 已派发）", action: scroll("down", "e9"), config: { rejectBeforeDispatch: (m) => (m === "Runtime.callFunctionOn" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "RED（同根因）：effect 内只派发过只读 resolveNode，确定无副作用，应 failed" },
  { group: "scroll", name: "不带 ref，evaluate 派发前被闸门拒绝", action: scroll("down"), config: { rejectBeforeDispatch: (m) => (m === "Runtime.evaluate" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "未派发 → failed 正确" },
  { group: "scroll", name: "只有 ref、ref 已脱离文档（scrollIntoView 前）", action: scroll(undefined, "e9"), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "此分支 element() 在 effect 之外 → failed 正确" },
  { group: "scroll", name: "只有 ref、把元素滚入视野", action: scroll(undefined, "e9"), expected: "done", note: "正常路径" },

  // ───────────────────────── wait_for ─────────────────────────
  { group: "wait_for", name: "textGone 超时", action: waitFor({ textGone: "Loading", seconds: 0.05 }), config: { answers: { innerText: "found" } }, expected: "failed", code: "WAIT_TIMEOUT", note: "只读轮询 → failed 正确" },
  { group: "wait_for", name: "轮询 evaluate 已派发后失败", action: waitFor({ text: "Done", seconds: 1 }), config: { failAfterDispatch: (m) => (m === "Runtime.evaluate" ? new Error("boom") : undefined) }, expected: "failed", code: "ACTION_FAILED", note: "evaluate 是只读观察，且在 effect 之外 → failed 正确" },
  { group: "wait_for", name: "轮询页面函数抛错", action: waitFor({ text: "Done", seconds: 1 }), config: { answers: { innerText: new Error("page broke") } }, expected: "failed", code: "ACTION_FAILED", note: "只读观察 → failed 正确" },
  { group: "wait_for", name: "轮询 evaluate 派发前被闸门拒绝", action: waitFor({ text: "Done", seconds: 1 }), config: { rejectBeforeDispatch: (m) => (m === "Runtime.evaluate" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "无副作用" },
  { group: "wait_for", name: "纯等待中途取消", action: waitFor({ seconds: 5 }), expected: "failed", abortDuring: 20, note: "纯等待无副作用 → failed 正确" },
  { group: "wait_for", name: "动作前已取消", action: { type: "wait_for", seconds: 5 }, expected: "failed", abortBefore: true, note: "steps 入口取消检查，无副作用" },

  // ───────────────────────── upload ─────────────────────────
  { group: "upload", name: "省略 ref 且没有待答选文件框", action: upload(["/w/a.pdf"]), expected: "failed", code: "NO_FILE_INPUT", note: "无副作用" },
  { group: "upload", name: "带 ref 但 ref 从未登记", action: upload(["/w/a.pdf"], "e404"), expected: "failed", code: "REF_STALE", note: "无副作用" },
  { group: "upload", name: "带 ref 且已脱离文档", action: upload(["/w/a.pdf"], "e9"), config: { answers: detached }, expected: "failed", code: "REF_STALE", note: "无副作用" },
  { group: "upload", name: "目标不是文件输入框", action: upload(["/w/a.pdf"], "e9"), config: { answers: { "not a file input": "not a file input" } }, expected: "failed", code: "NO_FILE_INPUT", note: "输入前检查" },
  { group: "upload", name: "文件输入框 disabled", action: upload(["/w/a.pdf"], "e9"), config: { answers: { "\"disabled\"": "disabled" } }, expected: "failed", code: "NOT_INTERACTABLE", note: "输入前检查" },
  { group: "upload", name: "多文件塞给单选输入框", action: upload(["/w/a.pdf", "/w/b.pdf"], "e9"), config: { answers: { "\"single\"": "single" } }, expected: "failed", code: "ACTION_FAILED", note: "输入前检查" },
  { group: "upload", name: "待答选文件框只收一个文件", action: upload(["/w/a.pdf", "/w/b.pdf"]), config: { fileChooser: { node: { backendNodeId: 9 }, multiple: false } }, expected: "failed", code: "ACTION_FAILED", note: "输入前检查" },
  { group: "upload", name: "setFileInputFiles 已派发后失败", action: upload(["/w/a.pdf"], "e9"), config: { failAfterDispatch: (m) => (m === "DOM.setFileInputFiles" ? new Error("boom") : undefined) }, expected: "unknown", code: "ACTION_FAILED", note: "文件可能已交给页面 → unknown 正确" },
  { group: "upload", name: "setFileInputFiles 派发前被闸门拒绝", action: upload(["/w/a.pdf"], "e9"), config: { rejectBeforeDispatch: (m) => (m === "DOM.setFileInputFiles" ? new HostMethodError("DIALOG_OPEN", "paused") : undefined) }, expected: "failed", code: "DIALOG_OPEN", note: "未派发 → failed 正确" },

  // ───────────────────────── dialog ─────────────────────────
  { group: "dialog", name: "没有待答弹窗", action: dialogAction(), expected: "failed", code: "NO_DIALOG", note: "无副作用" },
  { group: "dialog", name: "id 与当前弹窗不一致", action: dialogAction({ id: 99 }), config: { initialDialog: dialog }, expected: "failed", code: "DIALOG_MISMATCH", note: "无副作用" },
  { group: "dialog", name: "回答成功", action: dialogAction({ id: 7, accept: false }), config: { initialDialog: dialog }, expected: "done", note: "答复弹窗本身是动作目的" },

  // ───────────────────────── highlight 链 ─────────────────────────
  { group: "highlight", name: "标签可见：先量位置（含滚动元素）再发现 ref 失效", action: click({ ref: "e9" }), config: { shown: true, answers: detached }, expected: "failed", code: "REF_STALE", note: "高亮链的只读命令不置 effectStarted；页面可能被 scrollIntoView 滚动（视图副作用）→ failed 合理" },
];

async function runCase(testCase: Case): Promise<{ status: ActResult["steps"][number]["status"]; code: string | null; calls: string[] }> {
  const fixture = makeSession(testCase.config);
  const actions = [testCase.action];
  if (testCase.abortBefore) fixture.abort();

  let abortTimer: ReturnType<typeof setTimeout> | undefined;
  if (testCase.abortDuring !== undefined) abortTimer = setTimeout(() => fixture.abort(), testCase.abortDuring);

  let outcome: ActResult;
  try {
    outcome = await runActions(fixture.session, actions, fixture.signal);
  } finally {
    if (abortTimer) clearTimeout(abortTimer);
  }

  const step = outcome.steps[0];
  return {
    status: step.status,
    code: step.error?.code ?? null,
    calls: fixture.calls.map((call) => `${call.phase === "dispatched" ? "D" : "G"}:${call.method}`),
  };
}

describe("browser_act 动作结算矩阵（failed=确定未生效 / unknown=可能已生效）", () => {
  for (const testCase of cases) {
    it(`${testCase.group} / ${testCase.name} → ${testCase.expected}`, async () => {
      const { status, code, calls } = await runCase(testCase);
      expect(status, `calls=${calls.join(",")}`).toBe(testCase.expected);
      if (testCase.code) expect(code).toBe(testCase.code);
    });
  }
});
