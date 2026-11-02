import { HostMethodError } from "../host-link/methods";

/**
 * 浏览器方法经宿主工具链路交给后端的全部错误码：唯一登记处。后端按码给模型建议与卡片短提示
 * （`app/tools/tool_handler/browser/errors.py`），守卫测试（`tests/test_browser_contract_guard.py`）按正则读这个联合类型，
 * 码在这里有、后端没有对应文案就失败——不让新码悄悄退化成通用提示。
 *
 * `INVALID_PARAMS`、`CANCELLED`、`UNAVAILABLE` 与链路层同名：后端由 `host_error_observation` 统一解释。
 * 只给渲染进程的错误（IPC 直接显示给用户）不用这里的码。
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
  | "USER_ACTIVE"
  | "DIALOG_OPEN"
  | "NO_DIALOG"
  | "DIALOG_MISMATCH"
  | "REF_STALE"
  | "NOT_INTERACTABLE"
  | "WAIT_TIMEOUT"
  | "SCROLL_END"
  | "ACTION_FAILED";

export function browserError(code: BrowserErrorCode, message: string): HostMethodError {
  return new HostMethodError(code, message);
}
