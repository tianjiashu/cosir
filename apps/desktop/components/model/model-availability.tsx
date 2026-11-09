import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { RefreshCwIcon, SparklesIcon } from "lucide-react";

import { buttonVariants } from "@/components/ui/button";

import type { ModelCatalogSnapshot } from "@/lib/model/catalog";
import { cn } from "@/lib/utils";

/**
 * 模型目录的加载 / 出错 / 为空三态，所有模型选择器共用。
 *
 * 此前三个选择器各写一份，运行中的那份在目录为空时一直显示「加载模型…」。
 * 这里只认 model-catalog 的 status 与 models，不关心选择器自身的选中状态：
 * 目录已就绪且有模型时渲染 children，由调用方处理选中值。
 *
 * - inline：composer 内的紧凑样式；field：表单字段样式（带边框、与输入框等高）。
 * - 状态文字是用户必须看到的，不收成图标：单行、放不下时省略号，`title` 给全文。中文每个字之间都可断行，
 *   不禁止换行的话，在可收缩的 flex 行里会被压成一字一行。
 * - 目录为空或出错时附带「配置模型」入口，统一去配置中心的模型页（批量选模型需要整页空间）；
 *   离开任务页时任务页保活，返回不丢状态。
 */
export function ModelAvailability({
  status,
  modelCount,
  onRetry,
  variant = "inline",
  children,
}: {
  status: ModelCatalogSnapshot["status"];
  modelCount: number;
  onRetry: () => void;
  variant?: "inline" | "field";
  children: ReactNode;
}) {
  const group = variant === "field" ? "flex w-full min-w-0 items-center gap-1" : "inline-flex min-w-0 max-w-full items-center gap-1";
  const frame = variant === "field"
    ? "border-input flex h-9 w-full min-w-0 items-center gap-1.5 rounded-lg border px-3 text-sm"
    : "inline-flex h-8 min-w-0 items-center gap-1.5 px-2 text-xs";
  const configure = <Link to="/settings/models" className={buttonVariants({ variant: "link", size: "sm" })}>配置模型</Link>;
  const label = (text: string) => <span className="min-w-0 truncate" title={text}>{text}</span>;

  if (status === "loading") {
    return (
      <span role="status" className={cn(frame, "text-muted-foreground")}>
        <SparklesIcon className="size-3.5 shrink-0 animate-pulse" aria-hidden="true" />{label("加载模型…")}
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className={group}>
        <button type="button" onClick={onRetry} className={cn(frame, "text-destructive hover:bg-destructive/10 focus-ring rounded-lg")}>
          <RefreshCwIcon className="size-3.5 shrink-0" aria-hidden="true" />{label("模型加载失败，重试")}
        </button>
        {configure}
      </span>
    );
  }
  if (modelCount === 0) {
    return (
      <span className={group}>
        <span className={cn(frame, "text-muted-foreground")}>{label("暂无可用模型")}</span>
        {configure}
      </span>
    );
  }
  return <>{children}</>;
}
