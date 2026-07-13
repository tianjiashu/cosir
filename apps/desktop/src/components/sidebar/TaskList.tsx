/**
 * 任务列表组件。
 *
 * 展示任务列表，支持选中态和标题截断。
 * 第一版使用静态 mock 数据。
 *
 * @module components/sidebar/TaskList
 */

import { ListTodo, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import type { TaskStatus } from "@shared/task";

/** 任务数据接口。 */
export interface TaskItem {
  /** 任务唯一标识。 */
  id: string;
  /** 任务显示标题。 */
  title: string;
  /** 任务状态（复用 shared 契约，避免双源漂移）。 */
  status: TaskStatus;
}

/** TaskList 组件属性。 */
interface TaskListProps {
  /** 任务数据列表（第一版为 mock 数据）。 */
  tasks: TaskItem[];
  /** 当前选中任务 ID。 */
  activeId: string | null;
  /** 选中回调。 */
  onSelect: (id: string) => void;
}

/**
 * TaskList 任务列表组件。
 *
 * 渲染可点击的任务列表，当前选中项有明确高亮，
 * 长标题自动截断不撑破布局。
 */
export function TaskList({ tasks, activeId, onSelect }: TaskListProps) {
  return (
    <div className="px-2 py-1">
      {/* 标题 */}
      <div className="mb-1 flex items-center gap-1 px-2 py-1.5 text-xs font-medium uppercase tracking-wider text-muted-foreground">
        <ListTodo className="h-3.5 w-3.5" />
        已安排
      </div>

      {/* 列表 */}
      {tasks.map((task) => (
        <button
          key={task.id}
          onClick={() => onSelect(task.id)}
          title={task.title}
          className={cn(
            "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors",
            activeId === task.id
              ? "bg-accent text-accent-foreground"
              : "hover:bg-accent/50 text-muted-foreground",
          )}
        >
          <ChevronRight
            className={cn(
              "h-3.5 w-3.5 shrink-0 transition-transform",
              activeId === task.id && "rotate-90",
            )}
          />
          <span className="truncate">{task.title}</span>
        </button>
      ))}
    </div>
  );
}
