import { useMemo } from "react";

import { useAttachmentWorkspaceId } from "@/components/assistant-ui/elements/attachment-context";
import { ImageStrip } from "@/components/image/image-strip";
import { attachmentContentUrl } from "@/lib/assistant/attachments/image-src";
import { browserActionVerb } from "@/lib/assistant/tool-catalog";
import { tabLabel } from "@/lib/browser/tab-label";
import { useBrowserTab } from "@/lib/browser/host-state-store";
import { revealBrowserTab } from "@/lib/browser/panel-store";
import { cn } from "@/lib/utils";
import { readBrowserPageDisplay, type BrowserActStep, type BrowserPageDisplay } from "../browser-page-display";
import { CallOpenButton } from "../call-open-button";
import type { CallView } from "../call-view";
import { DetailSurface } from "../details/detail-view";

/** 地址与标题是调用当时的快照，不是事实源：标签页可能已经导航或关闭（关闭后只标注、不提供打开入口）。 */
function BrowserPageMeta({ display }: { display: BrowserPageDisplay; }) {
  const liveTab = useBrowserTab(display.tabId);
  const label = tabLabel(display.title, display.url);
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5" title={display.url}>
      <span className="truncate">{label}</span>
      {liveTab === undefined && <span className="shrink-0">· 标签页已关闭</span>}
    </span>
  );
}

/** 浏览器是独立面板、不进工作台：有没有工作区都能打开。 */
function OpenTabAction({ display }: { display: BrowserPageDisplay; }) {
  const liveTab = useBrowserTab(display.tabId);
  if (liveTab === undefined) return null;
  return <CallOpenButton label={`在浏览器面板打开：${display.title || display.url}`} onClick={() => revealBrowserTab(liveTab.taskId, display.tabId)} />;
}

/** 截图按对话图片的统一外观显示缩略图，地址按消息附件同一规则解析。 */
function Screenshot({ assetId }: { assetId: string; }) {
  const workspaceId = useAttachmentWorkspaceId();
  const src = attachmentContentUrl(assetId, workspaceId);
  const images = useMemo(() => [{ src, alt: "网页截图" }], [src]);
  return <DetailSurface className="p-2"><ImageStrip images={images} label="网页截图" /></DetailSurface>;
}

/** `browser_act` 的每一步：做完的照常、停下的那步标红带短提示、之后没执行的标灰。目标说明来自网页，只显示。 */
function ActSteps({ steps }: { steps: BrowserActStep[]; }) {
  return (
    <DetailSurface><ol className="flex flex-col gap-0.5" aria-label="动作步骤">
      {steps.map((step, index) => (
        <li key={index} className={cn("flex min-w-0 gap-1.5", step.status === "failed" && "text-destructive", step.status === "skipped" && "text-muted-foreground")}>
          <span className="text-muted-foreground shrink-0 tabular-nums">{index + 1}.</span>
          <span className="shrink-0">{browserActionVerb(step.action)}</span>
          {step.target && <span className="min-w-0 truncate" title={step.target}>{step.target}</span>}
          {step.status === "failed" && step.hint && <span className="shrink-0">· {step.hint}</span>}
          {step.status === "skipped" && <span className="shrink-0">· 未执行</span>}
        </li>
      ))}
    </ol></DetailSurface>
  );
}

/**
 * `browser-page`（`browser_navigate` / `browser_snapshot` / `browser_act` / `browser_screenshot` / `browser_handoff`）：
 * 元信息是页面标题（或地址），截图或动作步骤是展开体（有一步停下时默认展开）。交接要用户做的事（`handoff_reason`）
 * 由等待卡展示并承载交还。
 */
export const browserPageView: CallView = (data, context) => {
  const display = readBrowserPageDisplay(data);
  if (!display) return null;
  return {
    meta: context.inherited ? tabLabel(display.title, display.url) : <BrowserPageMeta display={display} />,
    actions: context.inherited ? null : <OpenTabAction display={display} />,
    detail: display.screenshot || display.steps?.length ? {
      defaultOpen: display.steps?.some(step => step.status === "failed") ?? false,
      render: () => display.screenshot ? <Screenshot assetId={display.screenshot} /> : <ActSteps steps={display.steps!} />,
    } : undefined,
  };
};
