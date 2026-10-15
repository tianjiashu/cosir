import type { TransportFailure } from "@/lib/assistant/use-task-assistant-transport-runtime";

/**
 * Transport 失败后的处置方式。
 *
 * - `presentation`：`dialog` 只用于用户提交被拒，需要用户感知并重新提交；其余失败
 *   都是订阅层面的中断，走状态条并由快照对账自行收敛。
 * - `restoreComposer`：命令没有被后端接受时，把草稿退回输入框，避免用户输入丢失。
 * - `snapshot`：`none` 不读快照；`import` 读取 canonical snapshot 覆盖本地投影；
 *   `import-then-recover` 在导入后若 Run 仍未结束，再进入有界 Transport recovery 重新订阅。
 */
export type TransportFailurePolicy = {
  presentation: "dialog" | "status";
  restoreComposer: boolean;
  snapshot: "none" | "import" | "import-then-recover";
};

/**
 * 按请求意图（而不是 HTTP 阶段）决定 Transport 失败的处置。
 *
 * attach 只是订阅既有 Run，无论被拒还是流中断，都只说明订阅没建立或断开，业务 Run
 * 本身不受影响，因此绝不能弹「发送失败」，必须回到 canonical snapshot 对账并在 Run
 * 仍运行时走有界 recovery；也不能重新发起 business resume 的 POST /assistant。
 * 提交命令被拒时 Run 尚未因该命令改变，弹窗并恢复草稿即可，无需读快照；命令已被接受
 * 但流中断时，Run 可能已推进，需要导入快照，后续收敛由 onFinish / recovery 负责。
 */
export function resolveTransportFailurePolicy(failure: TransportFailure): TransportFailurePolicy {
  if (failure.request === "attach") {
    return { presentation: "status", restoreComposer: false, snapshot: "import-then-recover" };
  }
  if (failure.phase === "request-rejected") {
    return { presentation: "dialog", restoreComposer: true, snapshot: "none" };
  }
  return { presentation: "status", restoreComposer: false, snapshot: "import" };
}
