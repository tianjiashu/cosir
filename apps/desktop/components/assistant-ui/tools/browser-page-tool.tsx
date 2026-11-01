import { useMemo } from "react";
import { ArrowUpRightIcon, GlobeIcon, HandIcon } from "lucide-react";

import { ImageStrip } from "@/components/image/image-strip";
import { useAttachmentWorkspaceId } from "@/components/assistant-ui/elements/attachment-context";
import { attachmentContentUrl } from "@/lib/assistant/attachments/image-src";
import { Button } from "@/components/ui/button";
import { useBrowserTab } from "@/lib/browser/tabs-store";
import { useHandBack } from "@/lib/handoff/use-hand-back";
import { cn } from "@/lib/utils";
import { useWorkbenchActions } from "@/lib/workbench/context";
import { readBrowserPageDisplay } from "./browser-page-display";
import type { ToolPartRendererProps } from "./tool-renderer-registry";
import { ToolStatus } from "./tool-status";
import { readToolArtifact } from "./types";

/**
 * 内置浏览器工具的卡片：页面标题与地址，点一下在浏览器面板里看实时页面；截图按对话图片的统一外观显示缩略图；交接时给出要用户做的事
 * 与「交还给 Agent」。
 *
 * 地址与标题是调用当时的展示快照，不是事实源：标签页可能已经导航或关闭（关闭后不再提供打开入口）。
 * 交还就是对这次 `browser_handoff` 调用作出 HITL 决定，按工具调用寻址，结果随 Task 订阅到达。
 */
export function BrowserPageTool({ artifact: rawArtifact, toolCallId, taskId, interactive = false }: ToolPartRendererProps) {
  const artifact = readToolArtifact(rawArtifact);
  const display = readBrowserPageDisplay(artifact.display_data);
  const actions = useWorkbenchActions();
  const liveTab = useBrowserTab(display?.tabId ?? "");
  const { handBack, returning } = useHandBack(taskId);
  const workspaceId = useAttachmentWorkspaceId();
  const screenshotSrc = display?.screenshot ? attachmentContentUrl(display.screenshot, workspaceId) : undefined;
  const screenshots = useMemo(() => (screenshotSrc ? [{ src: screenshotSrc, alt: "网页截图" }] : []), [screenshotSrc]);
  if (!display) return null;

  const title = display.title || display.url || "新标签页";
  const canOpen = actions !== null && liveTab !== undefined;
  const awaitingUser = display.handoffReason !== undefined && artifact.backendStatus === "pending";
  const open = () => actions?.openBrowser({ taskId: liveTab!.taskId, tabId: display.tabId });

  return (
    <div data-testid="browser-page-card" className={cn("bg-card flex min-w-0 flex-col gap-2 rounded-xl border px-3 py-2 shadow-sm", awaitingUser && "border-info/40")}>
      <div className="flex min-w-0 items-center gap-3">
        <span className="bg-info/10 text-info flex size-8 shrink-0 items-center justify-center rounded-full" aria-hidden="true">
          {display.handoffReason ? <HandIcon className="size-4" /> : <GlobeIcon className="size-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium" title={title}>{title}</p>
          <p className="text-muted-foreground truncate text-xs" title={display.url}>
            {display.tabId} · {liveTab === undefined ? "标签页已关闭" : display.url}
          </p>
        </div>
        <ToolStatus status={artifact.backendStatus} />
        {canOpen && (
          <Button type="button" variant="ghost" size="sm" className="h-7 shrink-0 gap-1 px-2" onClick={open} aria-label={`在浏览器面板打开：${title}`}>
            打开<ArrowUpRightIcon className="size-3.5" />
          </Button>
        )}
      </div>
      {display.handoffReason && (
        <div className="bg-muted/50 flex flex-wrap items-center gap-2 rounded-lg px-3 py-2 text-sm">
          <span className="min-w-0 flex-1">交给你操作：{display.handoffReason}</span>
          {awaitingUser && interactive && (
            <Button type="button" size="sm" className="h-7" disabled={returning} onClick={() => void handBack(toolCallId)}>
              {returning ? "交还中…" : "交还给 Agent"}
            </Button>
          )}
        </div>
      )}
      {screenshots.length > 0 && <ImageStrip images={screenshots} label="网页截图" />}
    </div>
  );
}
