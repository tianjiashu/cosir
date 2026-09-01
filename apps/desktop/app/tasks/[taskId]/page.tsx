import { TaskPage as TaskPageClient } from "@/components/task-page";

export default async function TaskPage({
  params,
}: {
  params: Promise<{ taskId: string }>;
}) {
  const { taskId } = await params;
  const parsedTaskId = Number(taskId);

  if (!Number.isInteger(parsedTaskId) || parsedTaskId <= 0) {
    return <div className="p-6 text-sm">任务标识无效。</div>;
  }

  return <TaskPageClient taskId={parsedTaskId} />;
}
