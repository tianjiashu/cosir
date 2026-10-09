import { requestRaw } from "@/lib/http/client";

/** 用户对一次等待请求作出的决定（与后端 `UserDecisionKind` 对齐）。 */
export type UserInputDecisionKind = "approve" | "reject" | "abort";

/** 单条用户决定；`data` 的形状由提问工具定义（后端只透传）。 */
export type UserInputDecision = {
  request_id: string;
  decision: UserInputDecisionKind;
  data?: Record<string, unknown>;
};

export type SubmitUserInputDecisionInput = {
  taskId: number;
  /** 处于等待用户输入的 Run；决定随该 Run 的续跑请求一起提交。 */
  runId: number;
  decisions: readonly UserInputDecision[];
};

/**
 * 提交 human-in-the-loop 决定并恢复等待中的 Run。
 *
 * 决定是**一次请求的输入**，不落库：它随续跑请求（同一批携带 `runId`）到达后端，被解析成
 * LangGraph `Command(resume=...)` 的载荷交给等待节点消费。因此本函数必须携带 `runId`——
 * 它与空 commands 的续跑语义等价，区别只在于额外带上了用户的结构化决定。
 *
 * 只负责请求序列化与 accepted barrier 校验；不订阅返回的事件流（UI 更新由卡片自身的
 * Run 状态查询与既有订阅负责）。
 */
export async function submitUserInputDecision(
  input: SubmitUserInputDecisionInput,
): Promise<void> {
  const response = await requestRaw("/assistant", {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      taskId: input.taskId,
      threadId: `task-${input.taskId}`,
      runId: input.runId,
      commands: [
        {
          type: "custom",
          name: "user-input-decision",
          commandId: `user-input-decision-${globalThis.crypto.randomUUID()}`,
          payload: { decisions: input.decisions },
        },
      ],
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Status ${response.status}: ${body}`);
  }
  try {
    await response.body?.cancel();
  } catch {
    // accepted barrier 已经成立：订阅关闭失败不能被解释成业务拒绝。
  }
}
