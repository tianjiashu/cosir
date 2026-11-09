import { PuzzleIcon } from "lucide-react";

import { basename, isDirectoryPath } from "@/lib/assistant/capsules";
import { InlineToken } from "@/components/ui/inline-token";
import { WorkspaceFileIcon } from "@/components/workspace-files/file-icon";

type ChipRemove = { label: string; disabled?: boolean; onClick: () => void };

/** Skill 胶囊：输入框与已发送消息共用，消息里不传 `remove`。 */
export function SkillChip({ name, remove, disabled = false }: {
  name: string;
  remove?: ChipRemove;
  disabled?: boolean;
}) {
  return (
    <InlineToken
      icon={<PuzzleIcon />}
      name={name}
      description={`Skill：${name}`}
      tone="skill"
      remove={remove && { ...remove, disabled: remove.disabled || disabled }}
    />
  );
}

/**
 * 本机文件胶囊：输入框与已发送消息共用，名称从路径推导，目录按尾斜杠区分并显示文件夹图标。
 * 消息里不传 `remove`。
 */
export function FileChip({ path, remove, disabled = false }: {
  path: string;
  remove?: ChipRemove;
  disabled?: boolean;
}) {
  const directory = isDirectoryPath(path);
  const name = `${basename(path)}${directory ? "/" : ""}`;
  return (
    <InlineToken
      icon={<WorkspaceFileIcon name={name} folder={directory} />}
      name={name}
      description={path}
      preserveExtension={!directory}
      remove={remove && { ...remove, disabled: remove.disabled || disabled }}
    />
  );
}
