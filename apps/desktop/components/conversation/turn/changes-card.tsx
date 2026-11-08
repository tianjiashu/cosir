import { memo } from "react";
import { DeferredDetail } from "../tools/deferred-detail";
import { PencilLineIcon } from "lucide-react";
import { DisclosureChevron } from "@/components/ui/disclosure-chevron";
import { DiffStat, FileDiff, fileChangeTag, hasTextDiff } from "@/components/conversation/tools/call-views/file-changes-view";
import type { ChangedFile, ChangesCard as ChangesCardLayout } from "@/lib/assistant/turn-layout";
import { fileKey, useDisclosure } from "@/lib/assistant/disclosure-store";
import { cn } from "@/lib/utils";
import { SURFACE_CARD_CLASS } from "./turn-tokens";

export type ChangesCardProps = { card: ChangesCardLayout; taskId: number; messageId: string };

function FileHistory({ file, taskId, messageId }: ChangesCardProps & { file: ChangedFile }) {
  const [open, setOpen] = useDisclosure(taskId, fileKey(messageId, file.path), false);
  const single = file.operations.length === 1 ? file.operations[0].change : null;
  return <li className="min-w-0">
    <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="hover:bg-muted flex min-h-8 w-full items-center gap-2 px-3 text-left text-2sm">
      <DisclosureChevron open={open} className="size-3" />
      <span className="min-w-0 flex-1 truncate font-mono" title={file.path}>{file.path}</span>
      <span className="text-muted-foreground">{single ? fileChangeTag(single.status) : `${file.operations.length} 次操作`}</span>
      {single && <DiffStat insertions={single.insertions} deletions={single.deletions} />}
    </button>
    <div hidden={!open}><DeferredDetail visible={open} render={() => <ol className="space-y-3 px-3 pb-3 pl-7">
      {file.operations.map((operation) => {
        const { change } = operation;
        return <li key={`${operation.toolCallId}:${change.index}`}>
          <p className="text-muted-foreground mb-1 text-xs">{fileChangeTag(change.status)}{change.new_path ? ` → ${change.new_path}` : ""}</p>
          {hasTextDiff(change) && <FileDiff change={change} />}
        </li>;
      })}
    </ol>} /></div>
  </li>;
}

/** Per-call operation history; no synthesized final status or net diff. */
export const ChangesCard = memo(function ChangesCard(props: ChangesCardProps) {
  const { card } = props;
  return <section data-slot="changes-card" aria-label="文件操作记录" className={cn(SURFACE_CARD_CLASS, "py-2")}>
    <header className="text-muted-foreground flex items-center gap-2 px-3 pb-1 text-2sm">
      <PencilLineIcon className="size-3.5 shrink-0" aria-hidden />文件操作 · 涉及 {card.totals.files} 个路径 · {card.totals.calls} 次调用
    </header>
    <ul className="m-0 list-none p-0">{card.files.map((file) => <FileHistory key={file.path} file={file} {...props} />)}</ul>
  </section>;
});
