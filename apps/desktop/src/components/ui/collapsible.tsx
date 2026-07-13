/**
 * Collapsible 组件（shadcn/ui 风格）。
 *
 * 基于 Radix UI Collapsible，用于工具调用卡片等可折叠区域。
 *
 * @module components/ui/collapsible
 */

import * as React from "react";
import * as CollapsiblePrimitive from "@radix-ui/react-collapsible";

const Collapsible = CollapsiblePrimitive.Root;

/** 折叠触发按钮。 */
const CollapsibleTrigger = CollapsiblePrimitive.CollapsibleTrigger;

/** 可折叠内容区域。 */
const CollapsibleContent = React.forwardRef<
  React.ElementRef<typeof CollapsiblePrimitive.CollapsibleContent>,
  React.ComponentPropsWithoutRef<typeof CollapsiblePrimitive.CollapsibleContent>
>(({ ...props }, ref) => (
  <CollapsiblePrimitive.CollapsibleContent ref={ref} {...props} />
));
CollapsibleContent.displayName = "CollapsibleContent";

export { Collapsible, CollapsibleTrigger, CollapsibleContent };
