import { FileIcon } from "@react-symbols/icons/utils";
import { FolderIcon, FolderOpenIcon } from "lucide-react";

export function WorkspaceFileIcon({
  name,
  folder = false,
  open = false,
}: {
  name: string;
  folder?: boolean;
  open?: boolean;
}) {
  if (folder) {
    const Icon = open ? FolderOpenIcon : FolderIcon;
    return <Icon className="size-4 shrink-0 text-muted-foreground" />;
  }
  return (
    <FileIcon
      fileName={name}
      autoAssign
      width={16}
      height={16}
      className="shrink-0"
    />
  );
}
