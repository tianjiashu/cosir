import { XIcon } from "lucide-react";
import {
  referenceName,
  type WorkspaceReference,
} from "@/lib/workspace-references/codec";
import { useWorkbenchStore } from "@/lib/workbench/store";
import { parentsOf, useFilesStore } from "@/lib/workspace-files/store";
import { WorkspaceFileIcon } from "@/components/workspace-files/file-icon";
import { useReferenceBridge } from "./reference-bridge";

/** 草稿和历史共用胶囊；片段预览来自消息正文，只有显式打开路径才访问当前文件。 */
export function ReferenceChip({
  reference,
  remove,
  disabled = false,
}: {
  reference: WorkspaceReference;
  remove?: () => void;
  disabled?: boolean;
}) {
  const bridge = useReferenceBridge();
  const name = referenceName(reference);
  const open = () => {
    if (!bridge?.workspaceId || !bridge.workspaceRoot) return;
    const id = bridge.workspaceId;
    useWorkbenchStore
      .getState()
      .openFiles(id, bridge.workspaceRoot, bridge.workspaceName);
    const files = useFilesStore.getState();
    if (reference.kind === "directory")
      files.patch(id, {
        browsing: true,
        treeVisible: true,
        expanded: [...parentsOf(reference.path), reference.path],
        reference: null,
      });
    else {
      files.open(id, reference.path, true);
      if (reference.content !== undefined) files.patch(id, { reference });
    }
  };
  return (
    <span
      contentEditable={false}
      className="inline-flex max-w-full items-center gap-1 rounded-full border bg-muted px-2 py-0.5 align-middle text-xs"
      title={reference.path || "."}
      data-workspace-reference={reference.kind}
    >
      <button
        type="button"
        className="inline-flex min-w-0 items-center gap-1 rounded focus-visible:outline-2 focus-visible:outline-ring"
        disabled={!bridge?.workspaceId}
        onMouseDown={(event) => event.preventDefault()}
        onClick={open}
        aria-label={`查看引用 ${name}`}
      >
        <WorkspaceFileIcon
          name={reference.path}
          folder={reference.kind === "directory"}
        />
        <span className="max-w-52 truncate">{name}</span>
      </button>
      {remove && (
        <button
          type="button"
          className="flex size-6 items-center justify-center rounded-full hover:bg-background"
          aria-label={`移除引用 ${name}`}
          disabled={disabled}
          onMouseDown={(event) => event.preventDefault()}
          onClick={remove}
        >
          <XIcon className="size-3" />
        </button>
      )}
    </span>
  );
}
