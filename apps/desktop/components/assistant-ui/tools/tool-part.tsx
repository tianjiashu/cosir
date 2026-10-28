import { memo } from "react";

import { DeferredResultActions } from "./deferred-result-actions";
import {
  resolveToolRenderer,
  type ToolPartRendererProps,
} from "./tool-renderer-registry";
import { readToolArtifact } from "./types";

export { routeToolPart } from "./tool-renderer-registry";

/**
 * 工具 part 的唯一展示入口。
 *
 * 路由选择由纯注册表完成，具体 renderer 的异常由调用方的
 * `PartRenderBoundary` 隔离；本组件不执行工具、不改变后端状态。延迟结果的操作行（后台执行中 /
 * 取消 / 转到后台）在这里对所有工具统一挂载，renderer 不感知。
 */
const ToolPartImpl = (props: ToolPartRendererProps) => {
  const Renderer = resolveToolRenderer(props.toolName, props.artifact).renderer;
  const { taskId, interactive = true } = props;
  // `data-tool-call-id` 供看板按调用定位到这张卡片。
  return (
    <div data-tool-call-id={props.toolCallId}>
      <Renderer {...props} />
      {interactive && (
        <DeferredResultActions
          taskId={taskId}
          toolCallId={props.toolCallId}
          artifact={readToolArtifact(props.artifact)}
        />
      )}
    </div>
  );
};

export const ToolPart = memo(ToolPartImpl);
