/**
 * 代码块组件。
 *
 * 第一版使用 `<pre><code>` + 浅灰样式展示代码片段，
 * 后续替换为 Monaco Editor。
 *
 * @module components/chat/CodeBlock
 */

import { Copy, Check } from "lucide-react";
import { useState, useCallback } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { logWarn } from "@/lib/logger";

/** 代码块组件属性。 */
interface CodeBlockProps {
  /** 代码文本内容。 */
  code: string;
  /** 编程语言标识（可选，用于语法高亮提示）。 */
  language?: string;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * CodeBlock 代码块组件。
 *
 * 使用等宽字体 + 浅色背景 + 圆角边框渲染代码，
 * 右上角提供复制按钮（点击后显示已复制状态）。
 *
 * TODO: 后续替换为 Monaco Editor（对齐开发计划 §1.3 预留项）。
 */
export function CodeBlock({ code, language, className }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      logWarn("代码块复制到剪贴板失败", {
        module: "CodeBlock",
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }, [code]);

  return (
    <div className={cn("group relative overflow-hidden rounded-md border border-border bg-slate-950 text-sm", className)}>
      {/* 头部：语言标签 + 复制按钮 */}
      <div className="flex items-center justify-between border-b border-border/20 bg-slate-900 px-3 py-1.5">
        <span className="text-xs text-slate-400">{language ?? "text"}</span>
        <Button
          variant="ghost"
          size="icon"
          className="h-6 w-6 opacity-0 transition-opacity group-hover:opacity-100"
          onClick={handleCopy}
        >
          {copied ? (
            <Check className="h-3.5 w-3.5 text-emerald-400" />
          ) : (
            <Copy className="h-3.5 w-3.5 text-slate-400" />
          )}
        </Button>
      </div>

      {/* 代码内容 */}
      <pre className="overflow-x-auto p-3">
        <code className="font-mono text-xs leading-relaxed text-slate-300">{code}</code>
      </pre>
    </div>
  );
}
