import { useMemo } from "react";
import { ArrowUpRightIcon } from "lucide-react";

import { useAttachmentWorkspaceId } from "@/components/assistant-ui/elements/attachment-context";
import { ImageStrip } from "@/components/image/image-strip";
import { Button } from "@/components/ui/button";
import { attachmentContentUrl } from "@/lib/assistant/attachments/image-src";
import { useBrowserTab } from "@/lib/browser/tabs-store";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { readBrowserPageDisplay, type BrowserPageDisplay } from "../browser-page-display";
import type { CallView } from "../call-view";

/** 地址与标题是调用当时的快照，不是事实源：标签页可能已经导航或关闭（关闭后只标注、不提供打开入口）。 */
function BrowserPageMeta({ display }: { display: BrowserPageDisplay }) {
  const liveTab = useBrowserTab(display.tabId);
  const label = display.title || display.url || "新标签页";
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5" title={display.url}>
      <span className="truncate">{label}</span>
      {liveTab === undefined && <span className="shrink-0">· 标签页已关闭</span>}
    </span>
  );
}

function OpenTabAction({ display }: { display: BrowserPageDisplay }) {
  const actions = useWorkbenchActions();
  const liveTab = useBrowserTab(display.tabId);
  if (actions === null || liveTab === undefined) return null;
  return (
    <Button
      type="button"
      variant="ghost"
      size="xs"
      className="text-muted-foreground hover:text-foreground shrink-0"
      onClick={() => actions.openBrowser({ taskId: liveTab.taskId, tabId: display.tabId })}
      aria-label={`在浏览器面板打开：${display.title || display.url}`}
    >
      打开<ArrowUpRightIcon aria-hidden="true" />
    </Button>
  );
}

/** 截图按对话图片的统一外观显示缩略图，地址按消息附件同一规则解析。 */
function Screenshot({ assetId }: { assetId: string }) {
  const workspaceId = useAttachmentWorkspaceId();
  const src = attachmentContentUrl(assetId, workspaceId);
  const images = useMemo(() => [{ src, alt: "网页截图" }], [src]);
  return <div className="bg-surface rounded-lg p-2"><ImageStrip images={images} label="网页截图" /></div>;
}

/**
 * `browser-page`（`browser_navigate` / `browser_snapshot` / `browser_act` / `browser_screenshot` / `browser_handoff`）：
 * 元信息是页面标题（或地址），截图是展开体。交接要用户做的事（`handoff_reason`）由等待卡展示并承载交还。
 */
export const browserPageView: CallView = {
  accepts: (data) => readBrowserPageDisplay(data) !== null,
  meta: (data) => <BrowserPageMeta display={readBrowserPageDisplay(data)!} />,
  body: (data) => {
    const display = readBrowserPageDisplay(data)!;
    return display.screenshot ? <Screenshot assetId={display.screenshot} /> : null;
  },
  actions: (data) => <OpenTabAction display={readBrowserPageDisplay(data)!} />,
};
