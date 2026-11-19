import type { ReactNode } from "react";
import { LinkTargetIcon } from "@/components/links/link-target-icon";
import { MiddleTruncate } from "@/components/ui/middle-truncate";
import { useOpenLink } from "@/components/links/use-open-link";
import type { LineRange } from "@/lib/links/resolve-link";
import { cn } from "@/lib/utils";
import { useOpenWorkspacePath } from "./use-open-workspace-path";

/**
 * 可在工作台文件面板打开的路径文本（工具卡片共用）。
 *
 * 路径不在当前工作区内或没有活动工作区时退化为同样样式的纯文本，不给出点了没反应的入口。
 * `children` 缺省时显示路径（`PathText`：放不下时省略中间目录、保住文件名）；`line` 给出时打开后定位到该行
 * （搜索命中、读取起始行）。
 * 路径前带对象图标。不放进调用行：行可能整行是按钮，那里打开文件走行尾的「打开」（`OpenWorkspaceFileButton`）。
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
  const content = (
    <>
      <LinkTargetIcon target={{ kind: "workspace", path, directory }} className="mr-1.5 size-3.5" />
      {children ?? <PathText path={path} />}
    </>
  );
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

/**
 * 放不下时先省略目录、保住文件名：文件名是最有用的部分，从尾部截断会最先丢掉它。目录段收缩系数极大，先被挤到
 * 只剩省略号；文件名本身还放不下时按全仓唯一的文件名截断规则（`MiddleTruncate`，保住扩展名）截；`suffix`
 * （行号 `:42`）不参与截断。不测量，纯弹性收缩。
 */
export function PathText({ path, suffix }: { path: string; suffix?: string }) {
  const cut = path.lastIndexOf("/") + 1;
  return (
    <span className="inline-flex max-w-full min-w-0 align-bottom">
      {/* 分段的 flex 子项在无障碍名称里会被空格隔开（「src/ re view.py」），所以显示部分对读屏隐藏、另给一份完整的。 */}
      <span aria-hidden="true" className="inline-flex min-w-0">
        {cut > 0 && <span className="min-w-0 shrink-[1000] truncate">{path.slice(0, cut)}</span>}
        <MiddleTruncate text={path.slice(cut)} className="min-w-0" />
        {suffix && <span className="shrink-0">{suffix}</span>}
      </span>
      <span className="sr-only">{path}{suffix}</span>
    </span>
  );
}

