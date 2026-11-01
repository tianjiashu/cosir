import type { ReactNode } from "react";
import { useOpenLink } from "@/components/links/use-open-link";
import type { LineRange } from "@/lib/links/resolve-link";
import { cn } from "@/lib/utils";
import { useOpenWorkspacePath } from "./use-open-workspace-path";

/**
 * 可在工作台文件面板打开的路径文本（工具卡片共用）。
 *
 * 路径不在当前工作区内或没有活动工作区时退化为同样样式的纯文本，不给出点了没反应的入口。
 * `children` 缺省时显示原始路径；`line` 给出时打开后定位到该行（搜索命中、读取起始行）。
 */
export function WorkspacePathLink({
  path,
  directory = false,
  line,
  children,
  className,
}: {
  path: string;
  /** 目录在文件树里展开定位，文件打开为标签。 */
  directory?: boolean;
  line?: number;
  children?: ReactNode;
  className?: string;
}) {
  const files = useOpenWorkspacePath();
  const links = useOpenLink("tool");
  const target = files?.resolve(path) ?? null;
  const content = children ?? path;
  if (target === null) {
    return <span className={cn("truncate", className)} title={path}>{content}</span>;
  }
  const lines: LineRange | undefined = line && !directory ? { start: line, end: line } : undefined;
  return (
    <button
      type="button"
      title={`在文件面板打开 ${target}`}
      onClick={() => links.open({ kind: "workspace", path: target, directory, lines })}
      className={cn(
        "min-w-0 truncate text-left underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-ring",
        className,
      )}
    >
      {content}
    </button>
  );
}
