/**
 * 文件链接组件。
 *
 * 展示可点击的文件路径样式（如 `docs/agent-v1-technical-plan.md`），
 * 使用等宽字体 + 链接色区分。
 *
 * @module components/chat/FileLink
 */

import { FileText } from "lucide-react";
import { cn } from "@/lib/utils";
import { logInfo } from "@/lib/logger";

/** 文件链接组件属性。 */
interface FileLinkProps {
  /** 文件路径（相对或绝对）。 */
  path: string;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * FileLink 文件链接组件。
 *
 * 渲染为可点击的内联文件路径，使用图标 + 等宽字体，
 * 对齐 Codex 桌面客户端的文件引用视觉风格。
 *
 * 第一版点击仅记录日志，后续接入 Tauri shell open 打开文件。
 */
export function FileLink({ path, className }: FileLinkProps) {
  /** 点击处理（第一版占位）。 */
  const handleClick = () => {
    logInfo("用户点击文件链接", { module: "FileLink", path });
    // TODO: 接入 Tauri shell open 或编辑器打开
  };

  return (
    <button
      onClick={handleClick}
      className={cn(
        "inline-flex items-center gap-1 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-primary transition-colors hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700",
        className,
      )}
    >
      <FileText className="h-3 w-3 shrink-0" />
      <span className="underline decoration-dotted">{path}</span>
    </button>
  );
}
