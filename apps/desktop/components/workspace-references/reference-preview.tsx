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
      <div className="space-y-2 border-b p-3 text-sm">
        <p className="font-medium">{referenceName(reference)}</p>
        <p className="break-all text-xs text-muted-foreground">
          {reference.path}
        </p>
        <p className="text-xs text-muted-foreground">
          引用片段 · 内容保存在消息正文中，行号是选取时的来源说明。
        </p>
        <Button
          size="sm"
          variant="outline"
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
