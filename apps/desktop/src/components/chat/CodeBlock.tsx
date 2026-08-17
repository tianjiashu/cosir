/**
 * 代码块组件。
 *
 * 第一版使用 `<pre><code>` + 浅灰样式展示代码片段，
 * 后续替换为 Monaco Editor。
 *
 * @module components/chat/CodeBlock
 */

import { Copy, Check } from "lucide-react";
import { useMemo, useState } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { MessageTypography } from "./messageTypography";
import { StreamingCaret } from "./StreamingCaret";
import { highlightCode } from "@/lib/markdown/highlight";
import { useCopyToClipboard } from "@/hooks/useCopyToClipboard";

/** 流式期触发折叠的行数阈值（超过则折叠）。 */
const CODE_FOLD_LINES = 12;
/** 流式期触发折叠的字符数阈值（超过则折叠）。 */
const CODE_FOLD_CHARS = 600;

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
 *   流式生成期间若代码超过行数/字符阈值则限高折叠，
 *   底部提供「展开完整代码」按钮，避免长代码撑爆滚动视图；
 *   非流式（生成完成）状态永不折叠。
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
 *   复制按钮会写入系统剪贴板并记录日志；
 *   展开按钮会更新组件内部展开状态。
 *
 * TODO: 后续替换为 Monaco Editor（对齐开发计划 §1.3 预留项）。
 */
export function CodeBlock({ code, language, streaming = false, isLastLeaf = false, className }: CodeBlockProps) {
  const [expanded, setExpanded] = useState(false);
  const { copied, copy } = useCopyToClipboard();

  // 仅需行数用于折叠判定：直接计数换行符，避免流式每帧 split 出整个行数组（纯垃圾分配）。
  const lineCount = useMemo(() => {
    let count = 1;
    for (let i = 0; i < code.length; i += 1) {
      if (code.charCodeAt(i) === 10) count += 1;
    }
    return count;
  }, [code]);
  const highlight = useMemo(() => highlightCode(code, language), [code, language]);
  const shouldFold = streaming === true && (lineCount > CODE_FOLD_LINES || code.length > CODE_FOLD_CHARS);
  const folded = shouldFold && !expanded;

  const handleCopy = () => {
    void copy(code);
  };

  return (
    <div className={cn("group relative min-w-0 overflow-hidden rounded-md border border-border bg-slate-950 text-sm", className)}>
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

      {/* 代码内容：流式期超阈值时限高折叠 */}
      <div className="relative">
        <pre className={cn("overflow-x-auto p-3", folded && "max-h-48 overflow-y-hidden")}>
          {highlight ? (
            <code
              className={cn(MessageTypography.code, "break-words text-slate-300")}
              dangerouslySetInnerHTML={{ __html: highlight.html }}
            />
          ) : (
            <code className={cn(MessageTypography.code, "break-words text-slate-300")}>{code}</code>
          )}
          <StreamingCaret show={streaming && isLastLeaf} />
        </pre>
        {folded ? (
          <div
            className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-slate-950 to-transparent"
            aria-hidden
          />
        ) : null}
      </div>

      {folded ? (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="w-full border-t border-border/20 bg-slate-900 py-1.5 text-xs text-slate-400 hover:text-slate-200"
        >
          展开完整代码
        </button>
      ) : null}
    </div>
  );
}
