import { WorkspaceFileIcon } from "@/components/workspace-files/file-icon";
import { InlineToken } from "@/components/ui/inline-token";
import type { RefCapsule } from "@/lib/assistant/capsules";
import {
  refDescription,
  refIsDirectory,
  refLineRange,
  refLocatorText,
  refName,
  refWorkspacePath,
} from "@/lib/workspace-references/ref-capsule";
import { useOpenWorkspacePath } from "./use-open-workspace-path";

/**
 * 工作区引用胶囊：输入框与已发送消息共用同一外观，消息里不传 `remove`。
 *
 * 点击打开当前文件并定位：行范围在文本阅读器里选中并居中，PDF 跳到对应页；目录在文件树里展开定位。
 * 引用只是指针，不携带选取时的正文，文件删除后打开会按实际的「文件不存在」提示。
 */
export function ReferenceChip({
  capsule,
  remove,
  disabled = false,
}: {
  capsule: RefCapsule;
  remove?: () => void;
  disabled?: boolean;
}) {
  const opener = useOpenWorkspacePath();
  const directory = refIsDirectory(capsule);
  const name = refName(capsule);
  const detail = refLocatorText(capsule);
  // 引用路径本身就是工作区相对路径（根目录为空串），不经 `resolve` 换算。
  const open = () => {
    if (!opener) return;
    if (directory) {
      opener.openDirectory(refWorkspacePath(capsule));
      return;
    }
    void opener.openFile(capsule.path, {
      lines: refLineRange(capsule) ?? undefined,
      page: capsule.page !== undefined ? Number(capsule.page) : undefined,
    });
  };
  return (
    <span data-workspace-reference="true">
      <InlineToken
        icon={<WorkspaceFileIcon name={capsule.path} folder={directory} />}
        name={name}
        detail={detail}
        description={refDescription(capsule)}
        preserveExtension={!directory}
        open={{ label: `查看引用 ${name}`, disabled: opener === null, onClick: open }}
        remove={remove ? { label: `移除引用 ${name}`, disabled, onClick: remove } : undefined}
      />
    </span>
  );
}
