import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { useOpenWorkspacePath } from "./use-open-workspace-path";

/**
 * 可在工作台文件面板打开的路径文本（工具卡片、对话正文共用）。
 *
 * 路径不在当前工作区内或没有活动工作区时退化为同样样式的纯文本，不给出点了没反应的入口。
 * `children` 缺省时显示原始路径。
 */
export function WorkspacePathLink({
  path,
  directory = false,
  children,
  className,
}: {
  path: string;
  /** 目录在文件树里展开定位，文件打开为标签。 */
  directory?: boolean;
  children?: ReactNode;
  className?: string;
}) {
  const opener = useOpenWorkspacePath();
  const target = opener?.resolve(path) ?? null;
  const content = children ?? path;
  if (!opener || target === null) {
    return <span className={cn("truncate", className)} title={path}>{content}</span>;
  }
  return (
    <button
      type="button"
      title={`在文件面板打开 ${target}`}
      onClick={() => (directory ? opener.openDirectory(target) : opener.openFile(target))}
      className={cn(
        "min-w-0 truncate text-left underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-ring",
        className,
      )}
    >
      {content}
    </button>
  );
}
