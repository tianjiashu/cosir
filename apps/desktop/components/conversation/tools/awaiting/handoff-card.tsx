import { readBrowserPageDisplay } from "@/components/conversation/tools/browser-page-display";
import { ToolIcon } from "@/components/conversation/tools/tool-icons";
import { Button } from "@/components/ui/button";
import { toolHeading, toolIconName } from "@/lib/assistant/tools/tool-catalog";
import { revealBrowserTab } from "@/lib/browser/panel-store";
import { useHandoffStore } from "@/lib/handoff/handoff-store";
import { useHandBack } from "@/lib/handoff/use-hand-back";
import type { CallViewContext } from "../call-view";
import { AwaitingFrame } from "./awaiting-frame";

/**
 * 交接（`browser_handoff`）等待交还时的卡片：说明是要用户做的事（`handoff_reason`）。「打开面板」按 snapshot
 * `handoffs` 里的登记把资源摆到用户面前；「交还」按工具调用寻址，后端重启后登记表为空、面板里没有提示条，
 * 这里仍是交还的唯一入口。
 */
export function HandoffCard({ sessionId, toolCallId, toolName, args, artifact, interactive }: CallViewContext) {
  const handoff = useHandoffStore((state) => state.sessions[sessionId]?.find((entry) => entry.toolCallId === toolCallId) ?? null);
  const { handBack, returning } = useHandBack(sessionId, toolCallId);
  const { title } = toolHeading(toolName, args, artifact.intent);
  const reason = readBrowserPageDisplay(artifact.display_data)?.handoffReason;

  return (
    <AwaitingFrame toolCallId={toolCallId} title={title} className="flex items-start gap-2.5">
      <ToolIcon name={toolIconName(toolName)} className="text-info mt-0.5 size-3.5" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{title}</p>
        {reason && <p className="text-muted-foreground mt-0.5 break-words">{reason}</p>}
      </div>
      {interactive && (
        <div className="flex shrink-0 items-center gap-1.5">
          {handoff !== null && (
            <Button type="button" size="xs" variant="secondary" onClick={() => revealBrowserTab(sessionId, handoff.resourceId)}>
              打开面板
            </Button>
          )}
          <Button type="button" size="xs" disabled={returning} onClick={() => void handBack(toolCallId)}>
            {returning ? "交还中…" : "交还"}
          </Button>
        </div>
      )}
    </AwaitingFrame>
  );
}
