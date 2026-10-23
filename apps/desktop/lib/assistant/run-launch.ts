import { isTerminalRunStatus, type InputDelivery, type TransportRunLaunch, type TransportState } from "@/lib/assistant/contract";
import { currentTransportRun } from "@/lib/assistant/transport-state-operations";

type LaunchRequest = {
  modelConfigId: number;
  reasoningEffort: string | null;
  banTools: readonly string[];
};

/**
 * 本次发送的发起配置是否与 Run 冻结的一致；与后端 `dispatch_policy.matches_run` 对齐。
 * banTools 按集合比较（后端入库时排序）。
 */
export function launchMatchesRun(launch: TransportRunLaunch, request: LaunchRequest): boolean {
  const requested = new Set(request.banTools);
  return launch.modelConfigId === request.modelConfigId
    && launch.reasoningEffort === request.reasoningEffort
    && launch.banTools.length === requested.size
    && launch.banTools.every((name) => requested.has(name));
}

/**
 * 发送的投递方式：活动 Run 期间配置不可更换（运行期配置锁定），配置不同的发送只能交给下一个
 * Run；其余沿用用户偏好。无活动 Run 时后端直接新开 Run，偏好不起作用。
 */
export function resolveSendDelivery(
  state: TransportState,
  request: LaunchRequest,
  preferred: InputDelivery,
): InputDelivery {
  const run = currentTransportRun(state);
  if (!run || isTerminalRunStatus(run.status)) return preferred;
  return launchMatchesRun(run.launch, request) ? preferred : "next_run";
}
