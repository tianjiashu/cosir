/**
 * Tooltip 组件（shadcn/ui 风格）。
 *
 * 基于 Radix UI Tooltip，用于图标按钮等紧凑元素的文字说明。
 *
 * @module components/ui/tooltip
 */

import * as React from "react";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { cn } from "@/lib/utils";

/**
 * Tooltip Provider（必须在祖先层级渲染一次）。
 */
const TooltipProvider = TooltipPrimitive.Provider;

/**
 * Tooltip 根组件。
 */
const Tooltip = TooltipPrimitive.Root;

/**
 * Tooltip 触发区域。
 */
const TooltipTrigger = TooltipPrimitive.Trigger;

/**
 * Tooltip 弹出内容。
 */
const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <TooltipPrimitive.Portal>
    <TooltipPrimitive.Content
      ref={ref}
      sideOffset={sideOffset}
      className={cn(
        "z-50 overflow-hidden rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2",
        className,
      )}
      {...props}
    />
  </TooltipPrimitive.Portal>
));
TooltipContent.displayName = TooltipPrimitive.Content.displayName;

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider };
