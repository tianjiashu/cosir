import { useEffect, useMemo, useState, type CSSProperties, type ReactElement } from "react";
import type { Language } from "@codemirror/language";
import { Decoration, Diff, Hunk, parseDiff } from "react-diff-view";
import type { FileData, HunkData, HunkTokens } from "react-diff-view";

import type { FileChange } from "@/lib/assistant/tools/file-changes";
import { frontendLog } from "@/lib/logging/frontend-log";
import { languageFor } from "@/lib/syntax/code-language";
import { diffTokens } from "@/lib/syntax/diff-tokens";
import { cn } from "@/lib/utils";
import { DetailBudget } from "./detail-budget";

type ParsedFileDiff =
  | { kind: "ready"; file: FileData; }
  | { kind: "empty"; }
  | { kind: "unavailable"; reason: "invalid" | "omitted"; };

export function parseFileDiff(change: FileChange): ParsedFileDiff {
  // null：超出展示上限、后端没有下发；空串：内容没变（如写入相同内容）。
  if (change.patch === null) return { kind: "unavailable", reason: "omitted" };
  if (!change.patch) return { kind: "empty" };
  const isRenamePatch = change.patch.includes("\nsimilarity index ")
    && change.patch.includes("\nrename from ")
    && change.patch.includes("\nrename to ");
  if (!change.patch.startsWith("diff --git ")) {
    return { kind: "unavailable", reason: "invalid" };
  }
  if (isRenamePatch) return { kind: "empty" };
  if (!change.patch.includes("\n--- ") || !change.patch.includes("\n+++ ")) {
    return { kind: "unavailable", reason: "invalid" };
  }

  try {
    const file = parseDiff(change.patch)[0];
    if (!file || file.hunks.length === 0) return { kind: "empty" };
    return { kind: "ready", file };
  } catch {
    return { kind: "unavailable", reason: "invalid" };
  }
}

/** 一个文件的 diff 正文；调用行展开体与「本轮改动」卡点开文件时共用。 */
export function FileDiff({ change }: { change: FileChange; }) {
  const parsed = useMemo(() => parseFileDiff(change), [change]);
  if (parsed.kind === "empty") return <p className="text-muted-foreground px-3 py-2 text-xs">没有文本差异</p>;
  if (parsed.kind === "unavailable") {
    return <p className="text-muted-foreground px-3 py-2 text-xs">{parsed.reason === "omitted" ? "改动过大，卡片里不保留 diff，可在文件面板查看" : "Diff 解析失败"}</p>;
  }
  return <ParsedDiff file={parsed.file} path={change.path} />;
}

/** 行内 `+a −b`；没有增删时不显示。 */
export function DiffStat({ insertions, deletions, className }: { insertions: number; deletions: number; className?: string; }) {
  if (insertions <= 0 && deletions <= 0) return null;
  return (
    <span className={cn("shrink-0 font-mono text-xs tabular-nums", className)}>
      {insertions > 0 && <span className="text-success">+{insertions}</span>}
      {insertions > 0 && deletions > 0 && " "}
      {deletions > 0 && <span className="text-destructive">−{deletions}</span>}
    </span>
  );
}

/**
 * 上下统一视图：对话列宽约 700px，左右分栏每侧只剩一半，长行几乎都要横向滚动。高度上限归 `DetailBudget`。
 *
 * 语法高亮与文件阅读器同源（按文件名取语言、同一张高亮类表）；语言包异步加载，加载前先只标字级改动。
 */
function ParsedDiff({ file, path }: { file: FileData; path: string; }) {
  const parser = useLanguageParser(path);
  const tokens = useMemo(() => tokensOrPlain(file.hunks, parser, path), [file, parser, path]);
  const style = { "--diff-gutter-digits": String(maxLineNumber(file.hunks)).length } as CSSProperties;
  return (
    <DetailBudget>
      <div style={style}>
        <Diff viewType="unified" diffType={file.type} hunks={file.hunks} tokens={tokens} className="aui-diff-view text-xs">
          {(hunks) => hunks.flatMap((hunk, index) => {
            const row: ReactElement[] = index > 0 ? [<Decoration key={`gap:${hunk.newStart}`}><HunkGap previous={hunks[index - 1]} hunk={hunk} /></Decoration>] : [];
            return [...row, <Hunk key={`${hunk.oldStart}:${hunk.newStart}:${index}`} hunk={hunk} />];
          })}
        </Diff>
      </div>
    </DetailBudget>
  );
}

/**
 * 两段改动之间的分隔：react-diff-view 不画，两段会贴在一起读成连续代码。行数按新侧算；上下文取 diff 头里
 * `@@ … @@` 之后的那一句（这段所在的函数或类），没有就只写行数。
 */
function HunkGap({ previous, hunk }: { previous: HunkData; hunk: HunkData; }) {
  const skipped = hunk.newStart - (previous.newStart + previous.newLines);
  const context = hunk.content.replace(/^@@[^@]*@@\s?/, "").trim();
  return <span className="flex min-w-0 gap-3"><span className="shrink-0">⋯ 跳过 {skipped} 行</span>{context && <span className="truncate font-mono">{context}</span>}</span>;
}

function useLanguageParser(path: string): Language["parser"] | null {
  const [loaded, setLoaded] = useState<{ path: string; parser: Language["parser"] } | null>(null);
  useEffect(() => {
    let disposed = false;
    void languageFor(path)
      ?.load()
      .then((support) => {
        if (!disposed) setLoaded({ path, parser: support.language.parser });
      })
      .catch((error) => {
        void frontendLog("WARNING", "diff_language_load_failed", "diff 语法高亮的语言加载失败，按纯文本显示", { data: { path }, error });
      });
    return () => {
      disposed = true;
    };
  }, [path]);
  return loaded?.path === path ? loaded.parser : null;
}

/** 语法高亮与字级改动（`lib/syntax/diff-tokens.ts`）；算不出来时记日志、退回不带 token 的纯文本 diff。 */
function tokensOrPlain(hunks: HunkData[], parser: Language["parser"] | null, path: string): HunkTokens | null {
  try {
    return diffTokens(hunks, parser);
  } catch (error) {
    void frontendLog("WARNING", "diff_tokenize_failed", "diff 高亮计算失败，按纯文本显示", { data: { path }, error });
    return null;
  }
}

/** 两侧最后一行的行号（行号栏按它的位数定宽）。 */
export function maxLineNumber(hunks: HunkData[]): number {
  const last = hunks[hunks.length - 1];
  return Math.max(last.oldStart + last.oldLines - 1, last.newStart + last.newLines - 1, 1);
}
