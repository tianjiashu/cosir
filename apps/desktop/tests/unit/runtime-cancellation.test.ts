import { beforeEach, describe, expect, it } from "vitest";
import { backgroundTarget, beginCommand, commandPending, decisionTarget, finishCommand, forgetCommands, markTaskRequestsUnsynced, reconcileCommands, reconcileTaskStatuses, releaseCommandObservation, stopTarget, useCommandRequests } from "@/lib/assistant/command-request-store";
import { activity } from "./transport-fixtures";

const current = () => useCommandRequests.getState().tasks[1];
beforeEach(() => forgetCommands([1]));

describe("跨界面的请求生命周期", () => {
  it("已发信号的停止保持到 canonical 落定，并跨挂载去重", () => {
    reconcileCommands(1, activity(7, "running"));
    const request = beginCommand(1, "task")!;
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task?.phase).toBe("syncing");
    expect(beginCommand(1, "task")).toBeNull();
    reconcileCommands(1, activity(7, "cancelled"));
    expect(current()?.task).toBeUndefined();
  });

  it("停下后新开的运行与迟到的 HTTP 响应不能复活旧的「停止中」", () => {
    reconcileCommands(1, activity(7, "running"));
    const request = beginCommand(1, "task")!;
    reconcileCommands(1, activity(7, "cancelled"));
    reconcileCommands(1, activity(8, "running"));
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task).toBeUndefined();
  });

  it("对话订阅断开时只把这个 Task 的停止转为待确认", () => {
    reconcileCommands(1, activity(7, "running"));
    reconcileCommands(2, activity(3, "running"));
    beginCommand(1, "task");
    beginCommand(2, "task");
    markTaskRequestsUnsynced(1);
    expect(current()?.task?.error).toContain("待确认");
    expect(useCommandRequests.getState().tasks[2]?.task?.error).toBeNull();
    forgetCommands([2]);
  });

  it("结果未知可以重试，旧请求的 HTTP 结果被忽略", () => {
    const first = beginCommand(1, "task")!;
    finishCommand(1, "task", first, "unknown", "未能确认");
    expect(commandPending(current()?.task)).toBe(false);
    const retry = beginCommand(1, "task")!;
    finishCommand(1, "task", first, "accepted");
    expect(current()?.task?.token).toBe(retry.token);
  });

  it("结果未知的决定同样可以重试：后端判重，重复提交只会 409", () => {
    const target = decisionTarget("c");
    const first = beginCommand(1, target)!;
    finishCommand(1, target, first, "unknown", "未能确认");
    expect(commandPending(current()?.[target])).toBe(false);
    expect(beginCommand(1, target)).not.toBeNull();
  });

  it("全局 Task 终态能收尾卡片已卸载的子 Task 停止", () => {
    const request = beginCommand(1, "task")!;
    reconcileTaskStatuses({ 1: "completed" });
    reconcileTaskStatuses({ 1: "running" });
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task).toBeUndefined();
  });

  it("状态流丢失后不再声称可能已是新 Run 的 Task 正在停止", () => {
    const request = beginCommand(1, "task")!;
    markTaskRequestsUnsynced();
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task?.error).toContain("待确认");
    expect(commandPending(current()?.task)).toBe(false);
  });

  it("HTTP 完成时用已观察到的事实收尾，不再等下一帧", () => {
    reconcileCommands(1, activity(7, "completed"));
    const request = beginCommand(1, "task")!;
    finishCommand(1, "task", request, "accepted");
    expect(current()?.task).toBeUndefined();
  });

  it("离开 Task 后旧事实失效，别处发起的停止不被它提前收尾", () => {
    reconcileCommands(1, activity(7, "completed"));
    releaseCommandObservation(1);
    const stop = beginCommand(1, "task")!;
    finishCommand(1, "task", stop, "accepted");
    expect(current()?.task?.phase).toBe("syncing");
    reconcileTaskStatuses({ 1: "completed" });
    expect(current()?.task).toBeUndefined();
  });
});

describe("同一调用的不同操作互不占用", () => {
  it("转后台在途时停止仍能发起，反之亦然", () => {
    expect(beginCommand(1, backgroundTarget("c"))).not.toBeNull();
    expect(beginCommand(1, stopTarget("c"))).not.toBeNull();
    expect(beginCommand(1, decisionTarget("c"))).not.toBeNull();
  });

  it("一种操作的状态不影响另一种的显示", () => {
    const stop = beginCommand(1, stopTarget("c"))!;
    finishCommand(1, stopTarget("c"), stop, "unknown", "停止未能确认");
    expect(current()?.[stopTarget("c")]?.error).toBe("停止未能确认");
    expect(current()?.[backgroundTarget("c")]).toBeUndefined();
  });

  it("被拒绝的调用级命令立即释放，可以再次发起", () => {
    const target = backgroundTarget("c");
    const request = beginCommand(1, target)!;
    finishCommand(1, target, request, "rejected");
    expect(current()?.[target]).toBeUndefined();
  });

  it("迟到的响应不能覆盖同一操作的新请求", () => {
    const target = stopTarget("c");
    const first = beginCommand(1, target)!;
    finishCommand(1, target, first, "unknown", "未能确认");
    const retry = beginCommand(1, target)!;
    finishCommand(1, target, first, "rejected");
    expect(current()?.[target]?.token).toBe(retry.token);
  });
});
