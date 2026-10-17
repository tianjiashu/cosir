import type { ComponentProps } from "react";
import { ReadonlyTextViewer } from "@/components/text-viewer/readonly-text-viewer";

/** 文件阅读器启用行号选区；通用文本展示不依赖工作区或引用业务。 */
export function CodePreview(
  props: Omit<
    ComponentProps<typeof ReadonlyTextViewer>,
    "ariaLabel" | "selectLines"
  >,
) {
  return (
    <ReadonlyTextViewer
      {...props}
      ariaLabel={`只读文件 ${props.name}`}
      selectLines
    />
  );
}
