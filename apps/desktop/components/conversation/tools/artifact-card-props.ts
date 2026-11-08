import type { ToolArtifact } from "@/lib/assistant/tool-artifact";

/** 产物区各卡（草稿卡、Team 卡）与等待卡里的审批表单共用的入参：按 Task 与调用寻址，不再经 assistant-ui 的 part props。 */
export type ArtifactCardProps = {
  /** 继承记录不读取原草稿的实时保存状态。 */
  inherited?: boolean;
  taskId: number;
  toolCallId: string;
  artifact: ToolArtifact;
  /** 只读展示面不提供写入交互（提交决定、停止 Team）。 */
  interactive?: boolean;
  /** 用户已请求停止整个 Task 的当前活动。 */
  taskCancelling?: boolean;
};
