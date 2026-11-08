import { ATTACHMENT_ASSET_ID } from "@/lib/assistant/attachments/attachment-locator";

/** `browser_act` 的一步：动作类型、目标说明（元素的角色与名称、坐标，来自网页文字）与结局。 */
export type BrowserActStep = {
  action: string;
  target: string | null;
  status: "done" | "failed" | "skipped" | "unknown";
  /** 停在这一步的短提示（后端分类映射）。 */
  hint?: string;
};

/** `display_data.kind = "browser-page"` 的载荷（后端 `app/tools/display/browser_display.py`）。 */
export type BrowserPageDisplay = {
  tabId: string;
  url: string;
  title: string;
  /** 截图的附件 asset id（`browser_screenshot`）。 */
  screenshot?: string;
  /** 交接给用户时要用户做的事（`browser_handoff`）。 */
  handoffReason?: string;
  /** `browser_act` 的每一步。 */
  steps?: BrowserActStep[];
};

const STEP_STATUSES = new Set(["done", "failed", "skipped", "unknown"]);

/** 校验展示载荷；形状不对返回 null，由注册表回落通用卡片。 */
export function readBrowserPageDisplay(value: unknown): BrowserPageDisplay | null {
  if (typeof value !== "object" || value === null) return null;
  const data = value as Record<string, unknown>;
  // `tab_id` 只用来查宿主状态镜像（查不到就是已关闭），不拼进地址，确认是字符串即可。
  if (data.kind !== "browser-page" || typeof data.tab_id !== "string") return null;
  if (typeof data.url !== "string" || typeof data.title !== "string") return null;
  const display: BrowserPageDisplay = { tabId: data.tab_id, url: data.url, title: data.title };
  if (typeof data.screenshot === "string" && ATTACHMENT_ASSET_ID.test(data.screenshot)) display.screenshot = data.screenshot;
  if (typeof data.handoff_reason === "string" && data.handoff_reason.trim()) display.handoffReason = data.handoff_reason;
  if (Array.isArray(data.steps)) {
    const steps = data.steps.map(readStep);
    if (steps.every((step) => step !== null)) display.steps = steps as BrowserActStep[];
  }
  return display;
}

function readStep(value: unknown): BrowserActStep | null {
  if (typeof value !== "object" || value === null) return null;
  const step = value as Record<string, unknown>;
  if (typeof step.action !== "string" || !STEP_STATUSES.has(step.status as string)) return null;
  if (step.target !== null && typeof step.target !== "string") return null;
  return {
    action: step.action,
    target: step.target,
    status: step.status as BrowserActStep["status"],
    ...(typeof step.hint === "string" ? { hint: step.hint } : {}),
  };
}

