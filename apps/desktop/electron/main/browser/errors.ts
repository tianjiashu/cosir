import { HostMethodError } from "../host-link/methods";

/**
 * 浏览器方法经宿主工具链路交给后端的全部错误码：唯一登记处。后端按码给模型建议与卡片短提示
 * （`app/tools/tool_handler/browser/errors.py`），守卫测试（`tests/test_browser_contract_guard.py`）按正则读这个联合类型，
 * 码在这里有、后端没有对应文案就失败——不让新码悄悄退化成通用提示。
 *
 * `INVALID_PARAMS`、`CANCELLED`、`UNAVAILABLE` 与链路层同名：后端由 `host_error_observation` 统一解释。
 * 用户在浏览器面板里的操作与 Agent 共用这些码：经 IPC 失败时按 `BROWSER_USER_WORDING` 换成给用户看的中文（`ipc.ts`）。
 */
export type BrowserErrorCode =
  | "INVALID_PARAMS"
  | "CANCELLED"
  | "UNAVAILABLE"
  | "TAB_NOT_FOUND"
  | "TAB_LIMIT"
  | "NAVIGATION_BLOCKED"
  | "NAVIGATION_FAILED"
  | "PAGE_CRASHED"
  | "PAGE_UNRESPONSIVE"
  | "TAB_BUSY"
  | "OPERATION_UNKNOWN"
  | "HANDED_OFF"
  | "USER_ACTIVE"
  | "DIALOG_OPEN"
  | "NO_DIALOG"
  | "DIALOG_MISMATCH"
  | "REF_STALE"
  | "OUT_OF_VIEWPORT"
  | "NO_FILE_INPUT"
  | "NOT_INTERACTABLE"
  | "WAIT_TIMEOUT"
  | "SCROLL_END"
  | "ACTION_FAILED"
  | "DEADLINE_EXCEEDED";

export function browserError(code: BrowserErrorCode, message: string): HostMethodError {
  return new HostMethodError(code, message);
}

/**
 * 每个码给用户看的说法（浏览器面板里的操作失败时显示）。按码写全：新增错误码时编译期就要求补上文案；具体原因
 * （英文的消息正文）只给 Agent 与日志。
 */
export const BROWSER_USER_WORDING: Record<BrowserErrorCode, string> = {
  INVALID_PARAMS: "请求不合法",
  CANCELLED: "操作已取消",
  UNAVAILABLE: "内置浏览器不可用",
  TAB_NOT_FOUND: "标签页已关闭",
  TAB_LIMIT: "标签页数量已达上限，请先关掉一些",
  NAVIGATION_BLOCKED: "只能打开 http 或 https 网址",
  NAVIGATION_FAILED: "页面加载失败",
  PAGE_CRASHED: "页面已崩溃，请刷新",
  PAGE_UNRESPONSIVE: "页面没有响应",
  TAB_BUSY: "页面上一次操作尚未结束",
  OPERATION_UNKNOWN: "页面输入状态未知，请关闭标签后重新打开",
  HANDED_OFF: "页面已交给用户，请先交还",
  USER_ACTIVE: "页面正在被使用",
  DIALOG_OPEN: "页面正在等待弹窗作答",
  NO_DIALOG: "弹窗已不在",
  DIALOG_MISMATCH: "弹窗已经换了",
  REF_STALE: "元素已不在页面上",
  OUT_OF_VIEWPORT: "位置超出了可见区域",
  NO_FILE_INPUT: "没有可上传文件的地方",
  NOT_INTERACTABLE: "元素不可操作",
  WAIT_TIMEOUT: "等待超时",
  SCROLL_END: "已经滚到头了",
  ACTION_FAILED: "操作失败",
  DEADLINE_EXCEEDED: "操作超时",
};
