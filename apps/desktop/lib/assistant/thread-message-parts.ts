import type { PartState, ThreadMessage } from "@assistant-ui/react";

import type { TransportMessagePart } from "@/lib/assistant/contract";
import { readToolArtifact } from "@/lib/assistant/tool-artifact";

/**
 * 把 assistant-ui 消息内容还原成回合投影（`turn-layout.projectTurn`）所需的 Transport part。
 *
 * 渲染与行高估算都从 assistant-ui 的消息对象出发，而分区规则只认 Transport 契约；这里是两者之间唯一的
 * 还原点：正文与思考只要文本与状态，调用从 `artifact` 取回后端状态、等待用户与展示数据，`args` 原样带上
 * （调用标题由工具名 + 参数查目录表得出）。
 */
export function transportPartsOf(content: ReadonlyArray<ThreadMessage["content"][number] | PartState>): TransportMessagePart[] {
  const parts: TransportMessagePart[] = [];
  for (const part of content) {
    if (part.type === "text") parts.push({ type: "text", text: part.text, status: partStatus(part.status) });
    else if (part.type === "reasoning") parts.push({ type: "reasoning", text: part.text, status: partStatus(part.status) });
    else if (part.type === "tool-call") {
      const artifact = readToolArtifact(part.artifact);
      parts.push({
        type: "tool-call",
        toolCallId: part.toolCallId,
        toolName: part.toolName,
        status: artifact.backendStatus,
        // 调用标题按工具名 + 参数查目录表算出（区头当前动作要用），所以参数要带上。
        args: (part.args ?? {}) as Record<string, unknown>,
        awaitingUser: artifact.awaitingUser,
        display_data: artifact.display_data,
        ...(artifact.childTaskId !== undefined ? { child_task_id: artifact.childTaskId } : {}),
        ...(artifact.team_run_id !== undefined ? { team_run_id: artifact.team_run_id } : {}),
      });
    }
  }
  return parts;
}

function partStatus(status: { type: string } | undefined): string | undefined {
  if (status?.type === "running") return "running";
  if (status?.type === "complete") return "completed";
  return undefined;
}
