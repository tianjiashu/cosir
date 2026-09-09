import type { ToolCallMessagePartProps } from "@assistant-ui/react";
import { ToolFallback } from "./tool-fallback";

/** @deprecated Use ToolFallback; kept as a compatibility export for local imports. */
export function UnknownTool(props: ToolCallMessagePartProps) {
  return <ToolFallback {...props} />;
}
