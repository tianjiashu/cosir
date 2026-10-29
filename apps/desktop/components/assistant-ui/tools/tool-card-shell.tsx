import type { ReactNode } from "react";
import { Collapsible, CollapsibleContent } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { DisclosureRow, DisclosureRowStatic } from "../elements/disclosure-row.aui";
import { ToolIcon } from "./tool-icons";
import { ToolStatus } from "./tool-status";
import { useToolDisclosure } from "./tool-disclosure";
import type { ToolArtifact } from "./types";

type ToolCardShellProps = {
  artifact: ToolArtifact;
  title: string;
  /** 进行中与成功态的行内摘要；失败、取消时外壳换成后端短提示，调用方不必区分。 */
  summary?: ReactNode;
  /** 成功态的展开内容；缺省（或静态声明 `expandable: false`）时渲染为不可展开的静态行。 */
  body?: ReactNode;
  defaultOpen?: boolean;
};

/**
 * 低噪音工具卡片的统一外壳：图标、标题、摘要、状态与折叠。
 *
 * 失败与取消只走一个通道：行内摘要位置显示 converter 收口后的 `artifact.error`，且不再
 * 可展开——展开内容只会重复同一句提示，失败态 `display_data` 也不携带成功态结果。
 * 各 renderer 只负责把 `display_data` 投影成摘要与展开内容，不再各自拼行布局和状态。
 * 深色终端卡片与审批、委派等专用卡片有自己的交互，不经过这里。
 */
export function ToolCardShell({ artifact, title, summary, body, defaultOpen = false }: ToolCardShellProps) {
  const status = artifact.backendStatus;
  const failed = status === "failed";
  const settledWithError = failed || status === "cancelled";
  const [open, setOpen] = useToolDisclosure(status, defaultOpen, { openWhileRunning: false });

  const meta = settledWithError ? (
    <span className={cn("max-w-[45%] truncate", failed ? "text-destructive" : "text-muted-foreground")}>
      {artifact.error ?? (failed ? "执行失败" : "已取消")}
    </span>
  ) : summary ? (
    <span className="text-muted-foreground max-w-[45%] truncate">{summary}</span>
  ) : undefined;

  const rowProps = {
    leading: <ToolIcon name={artifact.presentation.icon} aria-hidden="true" />,
    label: <span className={cn("text-sm font-medium", failed ? "text-destructive" : "text-foreground")}>{title}</span>,
    meta,
    trailing: <ToolStatus status={status} />,
  };

  if (settledWithError || body === undefined || body === null || artifact.presentation.expandable === false) {
    return <DisclosureRowStatic {...rowProps} />;
  }

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="group/tool-call">
      <DisclosureRow {...rowProps} />
      <CollapsibleContent className="ml-6 pl-2">{body}</CollapsibleContent>
    </Collapsible>
  );
}
