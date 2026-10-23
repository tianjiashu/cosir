import { useAuiState } from "@assistant-ui/react";

import { isTerminalRunStatus, type TransportRunLaunch, type TransportState } from "@/lib/assistant/contract";
import { currentTransportRun, readRuntimeTransportState } from "@/lib/assistant/transport-state-operations";

/** 运行期配置锁定时，模型与工具控件上的说明。 */
export const RUN_LAUNCH_LOCKED_HINT = "运行中不能更换模型配置，停止后可修改";

/**
 * 运行期配置锁定：Task 当前 Run 未到终态（运行或挂起）时返回它冻结的发起配置，否则 null。
 *
 * 锁定期间 composer 显示并发送这份配置，用户想换配置须先停止；锁定只由前端保证，前端不做配置比较，
 * 后端入队边界也不校验配置。
 */
export function activeRunLaunch(state: TransportState | null): TransportRunLaunch | null {
  const run = state === null ? undefined : currentTransportRun(state);
  return run === undefined || isTerminalRunStatus(run.status) ? null : run.launch;
}

/** 在 Assistant runtime 内订阅 {@link activeRunLaunch}；launch 对象随 snapshot 替换，引用稳定。 */
export function useActiveRunLaunch(): TransportRunLaunch | null {
  return useAuiState((state) => activeRunLaunch(readRuntimeTransportState(state.thread.state)));
}
