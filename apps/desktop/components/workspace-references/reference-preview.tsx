import {
  referenceName,
  type WorkspaceReference,
} from "@/lib/workspace-references/codec";
import { useFilesStore } from "@/lib/workspace-files/store";
import { Button } from "@/components/ui/button";
import { CodePreview } from "@/components/workspace-files/code-preview";

export function ReferencePreview({
  reference,
  workspaceId,
}: {
  reference: WorkspaceReference;
  workspaceId: number;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 路径已在阅读工具栏里；这里只用一行说明它是消息里的片段，不是文件当前内容。 */}
      <div role="note" className="mx-2 mb-1 flex shrink-0 items-center gap-2 rounded-md bg-muted/60 py-1 pr-1 pl-2 text-xs">
        <span className="min-w-0 flex-1 truncate" title="内容保存在消息正文中，行号是选取时的来源说明">
          <span className="font-medium">{referenceName(reference)}</span>
          <span className="text-muted-foreground"> · 消息里的引用片段，不是文件当前内容</span>
        </span>
        <Button
          size="sm"
          variant="ghost"
          className="shrink-0"
          onClick={() =>
            useFilesStore.getState().patch(workspaceId, { reference: null })
          }
        >
          打开当前文件
        </Button>
      </div>
      <div className="min-h-0 flex-1">
        <CodePreview
          firstLine={reference.startLine ?? 1}
          text={reference.content ?? ""}
          name={reference.path}
          wrap
          scrollTop={0}
          onScroll={() => {}}
        />
      </div>
    </div>
  );
}
