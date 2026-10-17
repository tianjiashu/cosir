import { requestRaw } from "@/lib/http/client";
import { parseHttpError } from "@/lib/http/errors";

/** 用户对一次等待请求作出的决定（与后端 `UserDecisionKind` 对齐）。 */
type UserInputDecisionKind = "approve" | "reject" | "abort";

/** 单条用户决定；`data` 的形状由提问工具定义（后端只透传）。 */
type UserInputDecision = {
  request_id: string;
  decision: UserInputDecisionKind;
  data?: Record<string, unknown>;
};

export type SubmitUserInputDecisionInput = {
  taskId: number;
  /** 处于等待用户输入的 Run；决定随该 Run 的续跑请求一起提交。 */
  runId: number;
  decision: UserInputDecision;
};

/**
 * 提交 human-in-the-loop 决定并恢复等待中的 Run。
 *
 * 决定是**一次请求的输入**，不落库：它随续跑请求（同一批携带 `runId`）到达后端，被解析成
 * LangGraph `Command(resume=...)` 的载荷交给等待节点消费。因此本函数必须携带 `runId`——
 * 它与空 commands 的续跑语义等价，区别只在于额外带上了用户的结构化决定。
 *
 * 只负责请求序列化与 accepted barrier 校验，不消费 POST 返回流。等待输入时原订阅已经结束，
 * 调用方在决定被接受后必须重新 attach 当前 Run，才能接收后续模型输出。
 *
 * 后端拒绝时抛 HttpError（经 parseHttpError 解析，不携带响应体原文），调用方用
 * userErrorMessage 转为界面文案。
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
          payload: { decision: input.decision },
        },
      ],
    }),
  });

  if (!response.ok) throw await parseHttpError(response);
  try {
    await response.body?.cancel();
  } catch {
    // accepted barrier 已经成立：订阅关闭失败不能被解释成业务拒绝。
  }
}
