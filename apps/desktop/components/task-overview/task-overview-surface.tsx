import { BackgroundTasksCard } from "./background-tasks-card";
import { useFocusedTaskId } from "./focused-task-context";

/** 工作台「任务概览」：纵向卡片栈，跟随当前聚焦的 Task；以后的上下文用量、改动等卡片加在这里。 */
export function TaskOverviewSurface() {
  const taskId = useFocusedTaskId();
  if (taskId === null) {
    return <div className="text-muted-foreground flex h-full items-center justify-center p-6 text-sm">打开一个对话后显示它的概览</div>;
  }
  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3" data-task-overview={taskId}>
      <BackgroundTasksCard taskId={taskId} />
    </div>
  );
}
