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
import { MessageTypography } from "./messageTypography";

/** 代码块组件属性。 */
interface CodeBlockProps {
  /** 代码文本内容。 */
  code: string;
  /** 编程语言标识（可选，用于语法高亮提示）。 */
  language?: string;
  /** 是否正在流式生成中（来自 projector 的块级 streaming 信号）。 */
  streaming?: boolean;
  /** 本代码块是否为整条消息的最后一个块级节点（决定是否承载流式光标）。 */
  isLastLeaf?: boolean;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * CodeBlock 代码块组件。
 *
 * 目的:
 *   使用等宽字体 + 浅色背景 + 圆角边框渲染代码，
 *   右上角提供复制按钮（点击后显示已复制状态）。
 *   当本块为流式消息的末块时，在代码尾部渲染光标。
 *
 * 参数:
 *   code - 代码文本；language - 语言标识；
 *   streaming - 是否流式生成中；isLastLeaf - 是否为消息末块；
 *   className - 额外 CSS 类名。
 *
 * 返回:
 *   代码块的 React 元素。
 *
 * 异常:
 *   不抛出异常（复制失败仅记录告警日志）。
 *
 * 副作用:
 *   复制按钮会写入系统剪贴板并记录日志。
 *
 * TODO: 后续替换为 Monaco Editor（对齐开发计划 §1.3 预留项）。
 */
export function CodeBlock({ code, language, streaming = false, isLastLeaf = false, className }: CodeBlockProps) {
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
        {streaming && isLastLeaf ? (
          <span className={cn(MessageTypography.caret, "ml-0.5")} aria-hidden />
        ) : null}
      </pre>
    </div>
  );
}
