import type { BackgroundTaskEntry, TransportToolStatus } from "../contract";

const labels: Record<TransportToolStatus, string> = {
  pending: "准备执行", running: "执行中", completed: "已完成",
  failed: "失败", cancelled: "已取消",
};

/** 调用生命周期、用户等待和后台工作各有事实源，在这里组合一次。 */
export function projectCallStatus(status: TransportToolStatus, awaitingUser = false, background = false, workStatus?: BackgroundTaskEntry["status"]) {
  const unfinished = status === "pending" || status === "running";
  if (!unfinished) return { label: labels[status], animated: false };
  if (awaitingUser) return { label: "等待你的操作", animated: false };
  if (background) return workStatus === "settling"
    ? { label: "后台结果收尾中", animated: false }
    : { label: "后台运行中", animated: true };
  return { label: labels[status], animated: true };
}

/** 后台已交出结果后，pending 只表示等待并入，不再允许停止或展示执行动画。 */
export function callIsExecuting(status: TransportToolStatus, background: boolean, workStatus?: BackgroundTaskEntry["status"]): boolean {
  return workStatus !== "settling" && (status === "running" || (background && status === "pending"));
}
