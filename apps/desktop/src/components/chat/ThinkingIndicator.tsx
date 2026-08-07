/**
 * 模型思考中指示器组件。
 *
 * 在用户已发出指令、请求已提交，但模型首 token 尚未返回的空窗期，
 * 于用户输入下方渲染一个明确的「思考中」状态，并附三点跳动动效，
 * 让用户立即获得「Agent 已收到指令、正在处理」的可视反馈，
 * 而非面对一段无任何进展暗示的静止空白。
 *
 * @module components/chat/ThinkingIndicator
 */

import { memo } from "react";
import { cn } from "@/lib/utils";
import { MessageTypography } from "./messageTypography";

/** ThinkingIndicator 组件属性。 */
interface ThinkingIndicatorProps {
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/** 三个跳动圆点的动画延迟序列（毫秒），错位相位实现依次弹跳。 */
const DOT_DELAYS = ["0ms", "150ms", "300ms"] as const;

/**
 * 模型思考中指示器。
 *
 * 目的:
 *   在「等待首 token」空窗期给出明确且带动效的状态提示，填补用户输入与
 *   首个 runtime 事件（thinking / assistant / tool）之间的视觉空档。
 *
 * 参数:
 *   className - 额外 CSS 类名。
 *
 * 返回:
 *   一个左对齐、带三点跳动动效的「思考中」状态元素。
 *
 * 异常:
 *   不抛出异常。
 *
 * 副作用:
 *   无（纯渲染）。动效由 CSS 驱动，不依赖定时器或副作用。
 */
export const ThinkingIndicator = memo(function ThinkingIndicator({ className }: ThinkingIndicatorProps) {
  return (
    <div className={cn("flex min-w-0", className)} data-testid="thinking-indicator">
      <div className="flex items-center gap-2 rounded-lg bg-muted/40 px-3 py-2 text-muted-foreground">
        <span className={cn(MessageTypography.secondary, "font-medium")}>思考中</span>
        <span className="flex items-center gap-1" aria-hidden data-testid="thinking-dots">
          {DOT_DELAYS.map((delay) => (
            <Dot key={delay} delay={delay} />
          ))}
        </span>
      </div>
    </div>
  );
});

/**
 * 单个跳动圆点。
 *
 * @param delay - 圆点弹跳动画的延迟相位（来自 {@link DOT_DELAYS}），用于错位实现依次跳动。
 * @returns 一个带 `thinking-dot` 动画类的装饰性圆点 span。
 */
function Dot({ delay }: { delay: string }) {
  return (
    <span
      className="h-1.5 w-1.5 rounded-full bg-muted-foreground/70 thinking-dot"
      style={{ animationDelay: delay }}
    />
  );
}
