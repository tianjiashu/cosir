/** `display_data.kind = "browser-page"` 的载荷（后端 `app/tools/display/browser_display.py`）。 */
export type BrowserPageDisplay = {
  tabId: string;
  url: string;
  title: string;
  /** 截图的附件 asset id（`browser_screenshot`）。 */
  screenshot?: string;
  /** 交接给用户时要用户做的事（`browser_handoff`）。 */
  handoffReason?: string;
};

const TAB_ID = /^t\d+$/;
const ASSET_ID = /^[0-9a-f]{64}$/;

/** 校验展示载荷；形状不对返回 null，由注册表回落通用卡片。 */
export function readBrowserPageDisplay(value: unknown): BrowserPageDisplay | null {
  if (typeof value !== "object" || value === null) return null;
  const data = value as Record<string, unknown>;
  if (data.kind !== "browser-page" || typeof data.tab_id !== "string" || !TAB_ID.test(data.tab_id)) return null;
  if (typeof data.url !== "string" || typeof data.title !== "string") return null;
  const display: BrowserPageDisplay = { tabId: data.tab_id, url: data.url, title: data.title };
  if (typeof data.screenshot === "string" && ASSET_ID.test(data.screenshot)) display.screenshot = data.screenshot;
  if (typeof data.handoff_reason === "string" && data.handoff_reason.trim()) display.handoffReason = data.handoff_reason;
  return display;
}
