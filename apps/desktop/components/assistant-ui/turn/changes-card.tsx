import { useState } from "react";
import { ChevronRightIcon, PencilLineIcon } from "lucide-react";

import { DiffStat, FileDiff, fileChangeTag, readChanges, type FileChange } from "@/components/assistant-ui/tools/views/file-changes-view";
import type { TransportToolDisplayData } from "@/lib/assistant/contract";
import type { ChangedFile, ChangesCard as ChangesCardLayout } from "@/lib/assistant/turn-layout";
import { cn } from "@/lib/utils";
import { SURFACE_CARD_CLASS } from "./turn-tokens";

export type ChangesCardProps = {
  card: ChangesCardLayout;
  /** 参与聚合的各次调用的展示数据，与 `card.callIndexes` 同序；点开文件时从这里找它的 diff。 */
  displays: ReadonlyArray<TransportToolDisplayData | null>;
};

/** 某个文件最后一次变更的载荷（同一文件多次写入时取最后一次，与聚合取「最终状态」一致）。 */
export function findLatestChange(displays: ReadonlyArray<TransportToolDisplayData | null>, file: ChangedFile): FileChange | null {
  for (let index = displays.length - 1; index >= 0; index -= 1) {
    const changes = readChanges(displays[index]?.changes);
    for (let position = changes.length - 1; position >= 0; position -= 1) {
      if (changes[position].path === file.path) return changes[position];
    }
  }
  return null;
}

/**
 * 「本轮改动」产物卡：按文件聚合的改动常显在结论之前（业界用户最在意、整轮折叠被投诉最多的部分，所以不折叠）。
 * 一块 `--surface` 底面，没有外框，文件行之间不画分隔线；点击文件展开它的 diff（复用调用行的 diff 内核）。
 */
export function ChangesCard({ card, displays }: ChangesCardProps) {
  const [openPaths, setOpenPaths] = useState<ReadonlySet<string>>(() => new Set());
  const toggle = (path: string) => setOpenPaths((current) => {
    const next = new Set(current);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    return next;
  });
  return (
    <section data-slot="changes-card" aria-label={`本轮改动 ${card.totals.files} 个文件`} className={cn(SURFACE_CARD_CLASS, "py-1.5")}>
      <header className="flex items-center gap-2 px-3 py-0.5 text-2sm">
        <PencilLineIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
        <span>本轮改动 {card.totals.files} 个文件</span>
        <DiffStat insertions={card.totals.additions} deletions={card.totals.deletions} className="ml-auto" />
      </header>
      <ul className="m-0 list-none p-0">
        {card.files.map((file) => {
          const open = openPaths.has(file.path);
          const change = open ? findLatestChange(displays, file) : null;
          const tag = fileChangeTag(file.status);
          return (
            <li key={file.path} className="min-w-0">
              <button
                type="button"
                aria-expanded={open}
                onClick={() => toggle(file.path)}
                className="hover:bg-muted flex min-h-7 w-full min-w-0 items-center gap-2 px-3 py-0.5 pl-[22px] text-left font-mono text-xs"
              >
                <ChevronRightIcon className={cn("text-muted-foreground size-3 shrink-0 transition-transform", open && "rotate-90")} aria-hidden="true" />
                <span className="min-w-0 truncate" title={file.newPath ? `${file.path} → ${file.newPath}` : file.path}>
                  {file.path}
                  {file.newPath && <><span className="text-muted-foreground mx-1" aria-hidden="true">→</span>{file.newPath}</>}
                </span>
                {tag && <span className="text-muted-foreground shrink-0 font-sans text-2xs">{tag}</span>}
                {file.status !== "deleted" && <DiffStat insertions={file.additions} deletions={file.deletions} className="ml-auto" />}
              </button>
              {open && (
                <div className="px-3 pb-1 pl-[34px]">
                  {change ? <FileDiff change={change} /> : <p className="text-muted-foreground px-3 py-2 text-xs">Diff 数据不可用</p>}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
