import type { TextSelection } from "@/components/text-viewer/readonly-text-viewer";
export type { TextSelection } from "@/components/text-viewer/readonly-text-viewer";
import { ContextMenu } from "radix-ui";
import { ChevronDownIcon } from "lucide-react";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { useReferenceBridge } from "./reference-bridge";
import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { AddReferenceButton } from "./add-reference-button";
import type { ReferenceInput } from "@/lib/workspace-references/prepare";
export function ReferenceSelectionMenu({
  workspaceId,
  input,
  children,
}: {
  workspaceId: number;
  input: ReferenceInput | null;
  children: ReactNode;
}) {
  const bridge = useReferenceBridge();
  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger asChild>{children}</ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Content className="z-50 min-w-40 rounded-md border bg-popover p-1 text-sm shadow-md">
          {[
            ["添加选区到对话", undefined],
            ["解释", "请解释这段内容。"],
            ["检查问题", "请检查这段内容的潜在问题。"],
            ["设计测试", "请为这段代码设计有意义的测试。"],
          ].map(([label, question]) => (
            <ContextMenu.Item
              key={label}
              disabled={!input || !bridge?.target || bridge.busy}
              className="cursor-default rounded px-2 py-1.5 outline-none focus:bg-muted data-[disabled]:opacity-50"
              onSelect={() =>
                input && void bridge?.add(workspaceId, [input], question)
              }
            >
              {label}
            </ContextMenu.Item>
          ))}
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}



export function selectedReference(
  path: string,
  selection: TextSelection | null,
  excerpt: string | null,
): ReferenceInput | null {
  if (excerpt) return { path, kind: "excerpt", content: excerpt };
  if (selection)
    return {
      path,
      kind: "snippet",
      content: selection.text,
      start_line: selection.startLine,
      end_line: selection.endLine,
    };
  return null;
}

/** 已选文字直接进入前端草稿；显式行范围按需复用文件读取接口。 */
export function SelectionActions({
  workspaceId,
  path,
  encoding,
  selection = null,
  excerpt = null,
}: {
  workspaceId: number;
  path: string;
  encoding?: string;
  selection?: TextSelection | null;
  excerpt?: string | null;
}) {
  const [rangeOpen, setRangeOpen] = useState(false);
  const [start, setStart] = useState("1");
  const [end, setEnd] = useState("1");
  const base = { path, encoding };
  const input = selectedReference(path, selection, excerpt);
  const valid =
    Number.isInteger(Number(start)) &&
    Number(start) >= 1 &&
    Number.isInteger(Number(end)) &&
    Number(end) >= Number(start);
  return (
    <div className="flex shrink-0 items-center">
      <AddReferenceButton workspaceId={workspaceId}
        inputs={[input ?? { path, kind: "file" }]}
        label={input ? "引用选区" : "引用文件"} />
      <Popover>
        <PopoverTrigger render={<Button size="icon-sm" variant="ghost" />} aria-label="更多引用方式" title="更多引用方式">
          <ChevronDownIcon className="size-3.5" />
        </PopoverTrigger>
        <PopoverContent align="end">
          <PopoverTitle>引用到对话</PopoverTitle>
          <AddReferenceButton workspaceId={workspaceId} inputs={[{ path, kind: "file" }]} label="添加文件到对话" />
          {input && <>
            <AddReferenceButton workspaceId={workspaceId} inputs={[input]} label={excerpt ? "添加摘录" : "添加选中片段"} />
            <AddReferenceButton workspaceId={workspaceId} inputs={[input]} label="解释" question="请解释这段内容。" />
            <AddReferenceButton workspaceId={workspaceId} inputs={[input]} label="检查问题" question="请检查这段内容的潜在问题。" />
            <AddReferenceButton workspaceId={workspaceId} inputs={[input]} label="设计测试" question="请为这段代码设计有意义的测试。" />
          </>}
          {encoding && <Button size="sm" variant="ghost" aria-expanded={rangeOpen} onClick={() => setRangeOpen(!rangeOpen)}>引用行范围</Button>}
          {rangeOpen && (
            <div className="flex flex-wrap items-center gap-2 py-2">
              <span>整文件行号</span>
              <input
                aria-label="引用起始行"
                className="w-20 rounded border bg-background px-2 py-1"
                type="number"
                min={1}
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
              <span>至</span>
              <input
                aria-label="引用结束行"
                className="w-20 rounded border bg-background px-2 py-1"
                type="number"
                min={Number(start)}
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
              <AddReferenceButton
                workspaceId={workspaceId}
                inputs={
                  valid
                    ? [
                      {
                        ...base,
                        kind: "snippet",
                        start_line: Number(start),
                        end_line: Number(end),
                      },
                    ]
                    : []
                }
                label="添加行范围"
              />
              <span className="text-muted-foreground">
                跨分段可用；Ctrl/Cmd 多选目录，Shift 连选行号。
              </span>
            </div>
          )}
        </PopoverContent>
      </Popover>
    </div>
  );
}
