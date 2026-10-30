import {
  referenceName,
  type WorkspaceReference,
} from "@/lib/workspace-references/codec";
import { WorkspaceFileIcon } from "@/components/workspace-files/file-icon";
import { InlineToken } from "@/components/ui/inline-token";
import { useOpenWorkspacePath } from "./use-open-workspace-path";

/**
 * 工作区引用胶囊：输入框与已发送消息共用同一外观，消息里不传 `remove`。
 * 片段预览来自消息正文，只有显式打开路径才访问当前文件。
 */
export function ReferenceChip({
  reference,
  remove,
  disabled = false,
}: {
  reference: WorkspaceReference;
  remove?: () => void;
  disabled?: boolean;
}) {
  const opener = useOpenWorkspacePath();
  const name = referenceName(reference);
  // 引用路径本身就是工作区相对路径（目录引用可能是根目录 ""），不经 `resolve` 换算。
  const open = () => {
    if (!opener) return;
    if (reference.kind === "directory") {
      opener.openDirectory(reference.path);
      return;
    }
    // 摘录引用带着选取时的正文，文件面板先展示这段快照，用户可再切到当前文件。
    opener.openFile(reference.path, reference.content !== undefined ? reference : undefined);
  };
  const fileName = reference.path.split("/").at(-1) || "工作区";
  const detail = reference.kind === "excerpt" ? " · 摘录"
    : reference.startLine ? ` · L${reference.startLine}–${reference.endLine}` : undefined;
  return (
    <span data-workspace-reference={reference.kind}>
      <InlineToken
        icon={<WorkspaceFileIcon name={reference.path} folder={reference.kind === "directory"} />}
        name={`${fileName}${reference.kind === "directory" ? "/" : ""}`}
        detail={detail}
        description={`${reference.path || "."}${detail ?? ""}`}
        preserveExtension={reference.kind !== "directory"}
        open={{ label: `查看引用 ${name}`, disabled: opener === null, onClick: open }}
        remove={remove ? { label: `移除引用 ${name}`, disabled, onClick: remove } : undefined}
      />
    </span>
  );
}
