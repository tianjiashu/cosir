import { readBrowserPageDisplay } from "@/components/conversation/tools/browser-page-display";
import { asRecord } from "@/components/conversation/tools/display-data-readers";
import { ToolIcon } from "@/components/conversation/tools/tool-icons";
import { Button } from "@/components/ui/button";
import { useHandoffStore } from "@/lib/handoff/handoff-store";
import { useHandBack } from "@/lib/handoff/use-hand-back";
import { revealBrowserTab } from "@/lib/browser/panel-store";
import type { ToolArtifact } from "@/lib/assistant/tools/tool-artifact";
import { toolHeading, toolIconName } from "@/lib/assistant/tools/tool-catalog";
import { cn } from "@/lib/utils";

export type AttentionCardProps = {
  sessionId: number;
  toolCallId: string;
  toolName: string;
  /** 模型给的原样参数；卡片标题按工具名 + 参数查目录表算出，与调用行同源。 */
  args: Record<string, unknown>;
  artifact: ToolArtifact;
  interactive?: boolean;
};

/** 等待卡的说明：交接要用户做的事（浏览器 `handoff_reason`），其余工具用后端短提示。 */
export function attentionDescription(artifact: ToolArtifact): string | null {
  const data = asRecord(artifact.display_data);
  const browser = readBrowserPageDisplay(data);
  if (browser?.handoffReason) return browser.handoffReason;
  if (typeof data.status_hint === "string" && data.status_hint.trim()) return data.status_hint;
  return null;
}

/**
 * 等待用户卡：`awaitingUser` 的调用从过程里提出来常显，是对话流里唯一带边框、唯一用信息蓝的容器
 * （设计 §5.8：需要人动作、可能停留很久，深浅主题都要第一眼看到）。
 *
 * 交接：「打开面板」按 snapshot `handoffs` 里的登记把资源摆到用户面前；「交还」按工具调用寻址，后端重启后
 * 登记表为空、面板里没有提示条，这里仍是交还的唯一入口。其余等待决定的
 * 工具只有标题与说明（决定由各自的界面给出）。
 */
export function AttentionCard({ sessionId, toolCallId, toolName, args, artifact, interactive = true }: AttentionCardProps) {
  const handoff = useHandoffStore((state) => state.sessions[sessionId]?.find((entry) => entry.toolCallId === toolCallId) ?? null);
  const { handBack, returning } = useHandBack(sessionId, toolCallId);
  const { title } = toolHeading(toolName, args, artifact.intent);
  const description = attentionDescription(artifact);
  // 交接是 HITL 调用：浏览器页面卡等待时对它作出决定即交还；重启后登记表为空也仍能交还。
  const canHandBack = interactive && (handoff !== null || artifact.display_data?.kind === "browser-page");

  return (
    <section
      data-slot="attention-card"
      data-tool-call-id={toolCallId}
      aria-label={`等你操作：${title}`}
      className={cn(
        "flex min-w-0 items-start gap-2.5 rounded-(--radius) px-3 py-2.5 text-2sm",
        "border border-[color-mix(in_oklab,var(--info)_38%,transparent)] bg-[color-mix(in_oklab,var(--info)_6%,transparent)]",
      )}
    >
      <ToolIcon name={toolIconName(toolName)} className="text-info mt-0.5 size-3.5" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold">{title}</p>
        {description && <p className="text-muted-foreground mt-0.5 break-words">{description}</p>}
      </div>
      {(handoff !== null || canHandBack) && (
        <div className="flex shrink-0 items-center gap-1.5">
          {handoff !== null && (
            <Button type="button" size="xs" variant="secondary" onClick={() => revealBrowserTab(sessionId, handoff.resourceId)}>
              打开面板
            </Button>
          )}
          {canHandBack && (
            <Button type="button" size="xs" disabled={returning} onClick={() => void handBack(toolCallId)}>
              {returning ? "交还中…" : "交还"}
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
