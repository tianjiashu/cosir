/**
 * 思考过程折叠展示组件。
 *
 * 模仿 Codex 风格：默认折叠显示「深度思考 >」，点击展开后显示完整思考内容
 * 并切换为「深度思考 ∨」。使用 lucide-react 的 ChevronRight 图标指示展开状态。
 *
 * 流式期（`streaming`）不显示折叠栏，直接展开累积内容并附带光标，
 * 让用户实时看到模型正在思考的内容；流式结束后回到默认折叠交互。
 *
 * @module components/chat/ThinkingBlock
 */

import { memo, useState, useEffect, useMemo, useRef } from "react";
import type { Components } from "react-markdown";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";
import { logInfo } from "@/lib/logger";
import { MessageTypography } from "./messageTypography";
import { StreamingCaret } from "./StreamingCaret";
import { MarkdownStream } from "./MarkdownStream";
import { buildMarkdownComponents } from "./AgentMessage";

/**
 * 思考展示粒度（方案 §阶段 3.4 展示开关的纯前端过滤能力，不改协议）。
 *
 * 后端 `thinking_display` 字段由主 Agent 协调共享协议扩展后，再接入设置面板；
 * 本组件先提供展示粒度过滤能力（UI 壳），供宿主按配置控制如何呈现思考内容。
 */
export type ThinkingDisplayMode = "full" | "summary" | "off";

/** ThinkingBlock 组件属性。 */
interface ThinkingBlockProps {
  /** 思考过程的完整文本内容。 */
  content: string;
  /** 展示粒度：full=完整、summary=摘要预览、off=关闭不渲染（默认 full）。 */
  display?: ThinkingDisplayMode;
  /** 是否正在流式生成中；true 时直接展开累积内容 + caret，不显示折叠栏。 */
  streaming?: boolean;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/** 「摘要」模式的预览长度（字符数）。 */
const SUMMARY_PREVIEW_LENGTH = 200;

/** 展开内容区的共用容器样式（流式与折叠展开保持一致视觉）。 */
const CONTENT_BOX_CLASS = "mt-2 rounded-md border border-muted bg-muted/50 px-3 py-2.5 text-muted-foreground";

/**
 * 可折叠的思考过程展示组件。
 *
 * 目的:
 *   非流式期默认折叠，点击标题栏切换展开/收起；流式期直接展开累积内容
 *   并在末尾渲染光标，避免用户在生成过程中看不到任何进展。
 *
 * 展示粒度（`display`）：
 *   - `full`：原样展示完整思考内容（默认，保持既有行为）；
 *   - `summary`：折叠态只展示内容前缀的摘要预览（`SUMMARY_PREVIEW_LENGTH`），
 *     避免完整思考文本长时间占据对话主区；
 *   - `off`：不渲染任何元素（等价于关闭思考展示，供宿主按展示开关过滤）。
 *   该过滤是纯前端展示能力（方案 §3.4 UI 壳），不改共享协议；后端
 *   `thinking_display` 接入后由宿主传入对应模式。
 *
 * 参数:
 *   content - 思考过程文本；
 *   display - 展示粒度（full / summary / off，缺省 full）；
 *   streaming - 是否处于流式生成中；
 *   className - 额外 CSS 类名。
 *
 * 返回:
 *   思考块的 React 元素；内容为纯空白或 display=off 时返回 null。
 *
 * 异常:
 *   不抛出异常。
 *
 * 副作用:
 *   挂载时通过 logInfo 记录内容长度与初始折叠态，用于排查
 *   「内容为空」与「有内容但被折叠隐藏」两类现象。
 */
export const ThinkingBlock = memo(function ThinkingBlock({
  content,
  display = "full",
  streaming = false,
  className,
}: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(false);
  const isEmpty = !content || content.trim().length === 0;

  // 复用 AgentMessage 的 markdown 组件映射（CodeBlock / FileLink / 行内代码），
  // 使「深度思考」与正式回复走同一套渲染链路，代码引用 / 文件路径 / 列表均可正确格式化。
  const components = useMemo<Components>(() => buildMarkdownComponents(streaming), [streaming]);

  // 「摘要」模式下仅展示内容前缀预览（完整思考不进入对话主区，降低视觉噪音）。
  const visibleContent =
    display === "summary" && content.length > SUMMARY_PREVIEW_LENGTH
      ? `${content.slice(0, SUMMARY_PREVIEW_LENGTH)}…`
      : content;

  // 注意：hooks 必须在任何 early return 之前调用，否则违反 React Hooks 规则。
  // 挂载日志只记录一次：流式期 content 按帧变化，若每次变化都写日志会形成
  // 「每帧一次 logInfo → Tauri IPC + 落盘」的洪峰，故用 ref 守卫保持 mounted 语义。
  const mountedLoggedRef = useRef(false);
  useEffect(() => {
    if (mountedLoggedRef.current) return;
    mountedLoggedRef.current = true;
    if (isEmpty) return;
    logInfo("thinking_block_mounted", {
      module: "ThinkingBlock",
      content_len: content.length,
      display,
      expanded_initial: false,
      streaming,
    });
  }, [content, isEmpty, streaming, display]);

  // 防御：纯空白内容不渲染任何元素（包括标题栏），避免出现空壳"深度思考"。
  // 投影器（projector）应已拦截此类条目，此处为最终防线。
  // display=off 表示关闭思考展示（方案 §3.4 展示开关），同样不渲染任何元素。
  if (isEmpty || display === "off") {
    return null;
  }

  // 流式期：不显示折叠栏，直接展示累积内容 + 光标。
  if (streaming) {
    return (
      <div className={cn("w-full", className)}>
        <div className={cn(CONTENT_BOX_CLASS, MessageTypography.secondary)}>
          <MarkdownStream
            content={visibleContent}
            streaming
            components={components}
            caret={<StreamingCaret show />}
            className="space-y-2"
          />
        </div>
      </div>
    );
  }

  return (
    <div className={cn("w-full", className)}>
      {/* 折叠/展开标题栏 */}
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        className="flex cursor-pointer items-center gap-1 text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronRight
          className={cn(
            "h-4 w-4 transition-transform duration-200",
            expanded && "rotate-90",
          )}
        />
        <span>深度思考</span>
      </button>

      {/* 展开内容区 */}
      {expanded ? (
        <div className={cn(CONTENT_BOX_CLASS, MessageTypography.secondary)}>
          <MarkdownStream
            content={visibleContent}
            streaming={false}
            components={components}
            className="space-y-2"
          />
        </div>
      ) : null}
    </div>
  );
});
