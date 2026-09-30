import { memo } from "react";
import {
  resolveToolRenderer,
  type ToolPartRendererProps,
} from "./tool-renderer-registry";

export { routeToolPart } from "./tool-renderer-registry";

/**
 * 工具 part 的唯一展示入口。
 *
 * 路由选择由纯注册表完成，具体 renderer 的异常由调用方的
 * `PartRenderBoundary` 隔离；本组件不执行工具、不改变后端状态。
 */
const ToolPartImpl = (props: ToolPartRendererProps) => {
  const Renderer = resolveToolRenderer(props.toolName, props.artifact).renderer;
  return <Renderer {...props} />;
};

export const ToolPart = memo(ToolPartImpl);
