import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { projectTurnHeading, type TurnHeadingPresentation } from "@/lib/assistant/turn/turn-heading";
import type { TransportOutcome } from "@/lib/assistant/contract";
import type { TurnLayout } from "@/lib/assistant/turn/turn-layout";
import { useElapsedSince } from "@/lib/assistant/turn/run-duration";
import { cn } from "@/lib/utils";

/** 计时只更新标题子树；正文、工具与产物不订阅墙钟。终态继续使用后端冻结的耗时。 */
export function LiveTurnHeading({ layout, outcome, startedAt, open, onToggle }: {
  layout: TurnLayout;
  outcome: TransportOutcome | null;
  startedAt: string | null | undefined;
  open: boolean;
  onToggle: () => void;
}) {
  const elapsed = useElapsedSince(startedAt, layout.phase !== "settled");
  return <TurnHeading heading={projectTurnHeading(layout, outcome, layout.phase === "settled" ? null : elapsed)} open={open} onToggle={onToggle} />;
}

/** The label and chevron form one left-aligned disclosure; a non-expandable heading is plain text. */
export function TurnHeading({ heading, open, onToggle }: { heading: TurnHeadingPresentation | null; open: boolean; onToggle: () => void }) {
  if (!heading) return null;
  const text = <span className={cn("min-w-0 wrap-anywhere", heading.failed && "text-destructive")}>{heading.label}</span>;
  return <div data-slot="turn-status" className="text-muted-foreground border-b border-border/60 pb-2 text-sm leading-5">
    {heading.expandable ? <button type="button" data-slot="process-head" aria-expanded={open}
      aria-label={`${heading.label}，${open ? "收起过程" : "展开过程"}`}
      className="hover:text-foreground flex min-h-7 min-w-0 max-w-full items-center gap-1 rounded-sm text-left outline-offset-4"
      onClick={onToggle}>
      {text}<DisclosureChevron open={open} />
    </button> : <div className="flex min-h-7 items-center gap-1">{text}</div>}
  </div>;
}
