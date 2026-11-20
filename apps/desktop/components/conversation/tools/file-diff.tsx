import { useEffect, useMemo, useState, type CSSProperties, type ReactElement, type ReactNode } from "react";
import { Decoration, Diff, Hunk, parseDiff } from "react-diff-view";
import type { FileData, HunkData, HunkTokens, RenderToken, TokenNode } from "react-diff-view";

import type { FileChange } from "@/lib/assistant/tools/file-changes";
import { frontendLog } from "@/lib/logging/frontend-log";
import { highlightLanguageOf } from "@/lib/syntax/code-language";
import { languageHighlighter, type LanguageHighlighter } from "@/lib/syntax/highlighter";
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
 * 着色与代码块、文件阅读器同源（着色服务，按文件名认语言）；语法异步加载，加载前先只标字级改动。
 */
function ParsedDiff({ file, path }: { file: FileData; path: string; }) {
  const highlighter = useHighlighter(path);
  const tokens = useMemo(() => tokensOrPlain(file.hunks, highlighter, path), [file, highlighter, path]);
  const style = { "--diff-gutter-digits": String(maxLineNumber(file.hunks)).length } as CSSProperties;
  return (
    <DetailBudget>
      <div style={style}>
        <Diff viewType="unified" diffType={file.type} hunks={file.hunks} tokens={tokens} renderToken={renderToken} className="aui-diff-view text-xs">
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

function useHighlighter(path: string): LanguageHighlighter | null {
  const [loaded, setLoaded] = useState<{ path: string; highlighter: LanguageHighlighter } | null>(null);
  useEffect(() => {
    let disposed = false;
    const lang = highlightLanguageOf(path);
    if (lang) void languageHighlighter(lang).then((highlighter) => {
      if (!disposed && highlighter) setLoaded({ path, highlighter });
    });
    return () => {
      disposed = true;
    };
  }, [path]);
  return loaded?.path === path ? loaded.highlighter : null;
}

/**
 * 画 diff 的词元：着色区间按 `properties.style` 写浅色与深色（深色由 `.shiki-token` 在 CSS 里切换），字级改动
 * 仍是 `diff-code-edit`。整棵树自己递归画：库的默认渲染只认 className，嵌在字级改动里的着色会丢。
 */
const renderToken: RenderToken = (token, _renderDefault, index) => renderNode(token, index);

function renderNode(node: TokenNode, key: number): ReactNode {
  if (node.type === "text") return node.value;
  const children = node.children?.length ? node.children.map(renderNode) : node.value;
  // 着色服务的样式只有 `color` 与 `--shiki-*` 变量，React 原样接受，不用转驼峰（单测锁住了键名）。
  if (node.type === "syntax") return <span key={key} className="shiki-token" style={node.properties.style as CSSProperties}>{children}</span>;
  return <span key={key} className="diff-code-edit">{children}</span>;
}

/** 语法高亮与字级改动（`lib/syntax/diff-tokens.ts`）；算不出来时记日志、退回不带 token 的纯文本 diff。 */
function tokensOrPlain(hunks: HunkData[], highlighter: LanguageHighlighter | null, path: string): HunkTokens | null {
  try {
    return diffTokens(hunks, highlighter);
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
