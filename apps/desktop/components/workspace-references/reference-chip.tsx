import { XIcon } from "lucide-react";
import {
  referenceName,
  type WorkspaceReference,
} from "@/lib/workspace-references/codec";
import { WorkspaceFileIcon } from "@/components/workspace-files/file-icon";
import { InlineToken } from "@/components/ui/inline-token";
import { useOpenWorkspacePath } from "./use-open-workspace-path";

/** 草稿和历史共用胶囊；片段预览来自消息正文，只有显式打开路径才访问当前文件。 */
export function ReferenceChip({
  reference,
  remove,
  disabled = false,
  variant = "message",
}: {
  reference: WorkspaceReference;
  remove?: () => void;
  disabled?: boolean;
  variant?: "message" | "composer";
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
  // 历史消息保留原有胶囊；输入框才使用与正文行高一致的紧凑视图。
  if (variant === "composer") {
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
        disabled={opener === null}
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
