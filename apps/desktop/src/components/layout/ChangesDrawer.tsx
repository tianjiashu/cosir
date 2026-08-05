/**
 * 对话上方的变更集折叠抽屉。
 *
 * 把原本位于右侧栏的 Changes Tab 上移到中央对话区顶部、输入框上方，以折叠形式
 * 呈现，默认收起，避免占用长对话的纵向空间。展开后分两个并列子区：
 *
 * - 任务列表：预留扩展位，待任务列表能力补齐后接入真实数据（见 {@link TaskListSlot}）。
 * - 文件列表：复用 {@link ChangesPanel} 提供检查点切换、批量保留 / 撤销与逐文件变更操作。
 *
 * 设计取舍：仅当存在活跃任务时才挂载（无任务则无变更集可展示）。折叠态只占一行
 * 高度，与对话流 / 输入框在视觉上以边框分隔，符合「对话上方折叠」的信息层级。
 *
 * @module components/layout/ChangesDrawer
 */

import { useState } from "react";
import { ChevronDown, FileDiff, ListChecks } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ChangesPanel } from "@/components/right-panel/ChangesTab";
import { useTaskStore, selectActiveTask } from "@/stores/taskStore";

/**
 * 任务列表子区占位组件。
 *
 * 当前项目尚未实现「对话上方任务列表」的真实数据源与交互，此处仅预留结构与接入点，
 * 不渲染任何伪数据。后续接入时：用真实任务列表组件替换本占位，并通过
 * ``useTaskStore`` / 任务查询 hook 获取「已完成 / 总数」计数显示在触发器右侧即可，
 * 外层 {@link ChangesDrawer} 的并列折叠布局无需改动。
 *
 * @returns 任务列表子区占位提示。
 */
function TaskListSlot() {
  return (
    <div className="px-1 py-6 text-center text-xs text-muted-foreground">
      任务列表即将上线
    </div>
  );
}

/**
 * 对话上方变更集折叠抽屉组件。
 *
 * 外层整体折叠（Changes），展开后含两个独立控制展开的子区：任务列表、文件列表。
 * 文件列表复用 ChangesPanel，其高度由根容器自身约束（max-h + overflow-hidden）使内部
 * VirtualList 获得确定高度、虚拟化滚动生效，避免大变更集退化为全量渲染。
 *
 * @returns 折叠态的变更集抽屉；无活跃任务时返回 null（不占用空间）。
 */
export function ChangesDrawer() {
  const [open, setOpen] = useState(false);
  // 单一派生订阅：活跃任务存在才挂载面板（避免无任务时的无意义请求与空挂载）。
  const activeTask = useTaskStore(selectActiveTask);

  if (!activeTask) {
    return null;
  }
  const activeTaskId = activeTask.task_id;

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="shrink-0 border-b border-border bg-muted/30">
      <CollapsibleTrigger className="flex w-full items-center gap-2 px-4 py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted/60">
        <FileDiff className="h-4 w-4 text-muted-foreground" />
        <span>Changes</span>
        <ChevronDown
          className={`ml-auto h-4 w-4 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`}
        />
      </CollapsibleTrigger>
      <CollapsibleContent className="overflow-hidden">
        {/* 展开后的并列子区：任务列表（预留） + 文件列表（复用 ChangesPanel）。 */}
        <div className="flex flex-col">
          <Collapsible className="border-t border-border">
            <CollapsibleTrigger className="group flex w-full items-center gap-2 px-4 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted/60">
              <ListChecks className="h-3.5 w-3.5" />
              <span>任务列表</span>
              {/* data-state 由 Radix 加在 Trigger(button) 上，需用 group-data-[state=open] 才能影响 chevron 子元素 */}
              <ChevronDown className="ml-auto h-3.5 w-3.5 transition-transform group-data-[state=open]:rotate-180" />
            </CollapsibleTrigger>
            <CollapsibleContent className="overflow-hidden px-4 pb-3">
              <TaskListSlot />
            </CollapsibleContent>
          </Collapsible>

          <Collapsible className="border-t border-border" defaultOpen>
            <CollapsibleTrigger className="group flex w-full items-center gap-2 px-4 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted/60">
              <FileDiff className="h-3.5 w-3.5" />
              <span>文件列表</span>
              <ChevronDown className="ml-auto h-3.5 w-3.5 transition-transform group-data-[state=open]:rotate-180" />
            </CollapsibleTrigger>
            <CollapsibleContent className="overflow-hidden">
              <ChangesPanel
                taskId={activeTaskId}
                // 抽屉模式：根容器自带最大高度并裁剪溢出，使内部 VirtualList 拿到确定高度，
                // 虚拟化滚动生效；同时与触发器以边框分隔。
                // 注：max-h-[40vh] 为视口相对的自适应上限（非固定 px），全项目唯一一处，
                // 故意不收口到 tokens.ts 的 Panel.codeBlockMaxHeight（px 固定值），因其含义是
                // 「不挤压对话流、按视口比例自适应防溢出」，属 UI 规范 §2.1 允许的一次性自适应例外。
                className="flex max-h-[40vh] min-h-0 flex-col gap-2 overflow-hidden border-t border-border p-3"
              />
            </CollapsibleContent>
          </Collapsible>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
