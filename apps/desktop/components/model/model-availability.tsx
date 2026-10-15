"use client";

import type { ReactNode } from "react";
import { RefreshCwIcon, SparklesIcon } from "lucide-react";

import type { ModelCatalogSnapshot } from "@/lib/model-catalog";
import { cn } from "@/lib/utils";

/**
 * 模型目录的加载 / 出错 / 为空三态，所有模型选择器共用。
 *
 * 此前三个选择器各写一份，运行中的那份在目录为空时一直显示「加载模型…」。
 * 这里只认 model-catalog 的 status 与 models，不关心选择器自身的选中状态：
 * 目录已就绪且有模型时渲染 children，由调用方处理选中值。
 *
 * - inline：composer 内的紧凑样式；field：表单字段样式（带边框、与输入框等高）。
 * - configure：目录为空或出错时提供的去配置入口（模型配置对话框或配置中心链接）。
 */
export function ModelAvailability({
  status,
  modelCount,
  onRetry,
  configure,
  variant = "inline",
  children,
}: {
  status: ModelCatalogSnapshot["status"];
  modelCount: number;
  onRetry: () => void;
  configure?: ReactNode;
  variant?: "inline" | "field";
  children: ReactNode;
}) {
  const group = variant === "field" ? "flex w-full items-center gap-1" : "inline-flex items-center gap-1";
  const frame = variant === "field"
    ? "border-input flex h-9 w-full items-center gap-1.5 rounded-lg border px-3 text-sm"
    : "inline-flex h-8 items-center gap-1.5 px-2 text-xs";

  if (status === "loading" || status === "idle") {
    return (
      <span role="status" className={cn(frame, "text-muted-foreground")}>
        <SparklesIcon className="size-3.5 animate-pulse" aria-hidden="true" />加载模型…
      </span>
    );
  }
  if (status === "error") {
    return (
      <span className={group}>
        <button type="button" onClick={onRetry} className={cn(frame, "text-destructive hover:bg-destructive/10 focus-ring rounded-lg")}>
          <RefreshCwIcon className="size-3.5" aria-hidden="true" />模型加载失败，重试
        </button>
        {configure}
      </span>
    );
  }
  if (modelCount === 0) {
    return (
      <span className={group}>
        <span className={cn(frame, "text-muted-foreground")}>暂无可用模型</span>
        {configure}
      </span>
    );
  }
  return <>{children}</>;
}
