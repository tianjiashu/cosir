/**
 * Task 维度元数据选择条（TaskHeaderBar）。
 *
 * 组合 AgentSelector + ModelSelector + ProviderSettingsDialog：
 * - AgentSelector：当前任务创建时使用的 Agent 标识（store: `useTaskStore.selectedAgentId`）。
 * - ModelSelector：当前任务创建/追加时使用的模型（null 表示未选择，需显式选择；
 *   store: `useTaskStore.selectedModelName`，已含 localStorage 持久化）。
 * - ProviderSettingsDialog：ModelSelector 的「配置模型 / 管理厂商」入口所触发的受控
 *   对话框；局部 useState 自治，**不**下沉到全局 store——该对话框的开关属于组件组合
 *   内部协调，应被具体消费方持有，复用 TaskHeaderBar 的多个调用点天然隔离。
 *
 * 单一职责边界（明确「不做什么」，约束后续维护者）：
 * - 不负责 workspace 切换（属于 NewTaskPage / Sidebar 各自职责）。
 * - 不负责输入区与发送逻辑（属于 InputBar 职责）。
 * - 不决定下游 store 是否就绪；只读取 store 事实源 + 触发 store 写入。
 * - 不读 activeTaskId / activeWorkspaceId 等上下文，仅暴露「task 创建时的配置入口」，
 *   与「哪个 task 正在聊」解耦——ChatPanel 顶部也展示同一条，复用同一事实源。
 *
 * 视觉：紧凑态 `h-8`，与原 InputBar 内的 AgentSelector / ModelSelector 折叠态同等级；
 * 默认右对齐，永远不抢主视觉重心，仅作为「任务维度元数据」入口。
 *
 * 未来工作（不在本组件首次合入范围）：
 * - 窄屏 fallback：当视口宽度 < 1024px 时折叠为仅图标按钮（仅 Bot / Cpu），保留点击展开。
 *   当前实现以桌面常开为主，窄屏场景待后续 PR；预留 hook 点为外层容器的 `className`
 *   注入（消费者可经 CSS `@container` 控制）。
 *
 * @module components/chat/TaskHeaderBar
 */

import { useState } from "react";
import { AgentSelector } from "@/components/chat/AgentSelector";
import { ModelSelector } from "@/components/chat/ModelSelector";
import { ProviderSettingsDialog } from "@/components/settings/ProviderSettingsDialog";
import { logInfo } from "@/lib/logger";
import { cn } from "@/lib/utils";

/** TaskHeaderBar 组件属性。 */
export interface TaskHeaderBarProps {
  /**
   * 可选的额外 CSS 类名，外层容器（注入后端 dexie）和消费者可借此调整对齐/边距。
   *
   * 默认无内边距；视觉节奏由父容器约束。
   */
  className?: string;
  /**
   * 受控的配置中心开关状态（可选）。未提供时组件内部 useState 自治；
   * 提供后由宿主持有状态，可响应发送前校验（guardSend openSettings）联动打开。
   */
  settingsOpen?: boolean;
  /** 受控开关的回调（可选）：open=true 打开、false 关闭；与 settingsOpen 成对使用。 */
  onSettingsOpenChange?: (open: boolean) => void;
}

/**
 * Task 维度元数据选择条组件。
 *
 * 不读取 activeTaskId / activeWorkspaceId：消费方可以是 NewTaskPage（无活跃任务）
 * 也可以是 ChatPanel（有活跃任务），事实源仅来自 `useTaskStore.selectedAgentId` 与
 * `useTaskStore.selectedModelName`——所选配置用于「下一次任务创建 / turn 创建」，
 * 不被当前活跃任务所限定，与「正在聊的任务用什么模型」二者语义重合时也合法（已落
 * 全局 store 一致即可）。
 *
 * ProviderSettingsDialog 的开关状态默认组件内部协调，不下沉到 store：
 * - 该对话框仅由 ModelSelector 的「配置模型 / 管理厂商」入口触发，无需在
 *   任何全局边界外被读；
 * - 多个 TaskHeaderBar 实例并存时（如未来 split view）各自的对话框生命周期独立。
 * - 宿主也可经可选受控 props（settingsOpen / onSettingsOpenChange）接管开关，
 *   用于响应发送前校验（guardSend openSettings）的联动打开。
 *
 * @param props - 组件属性。
 * @param props.className - 可选的外层类名注入（对齐、间距等）。
 * @param props.settingsOpen - 可选受控开关状态；未提供时内部 useState 自治。
 * @param props.onSettingsOpenChange - 可选受控开关回调；与 settingsOpen 成对使用。
 * @returns TaskHeaderBar 的 React 元素。
 *
 * @sideeffect
 * - 渲染时触发 AgentSelector 首次挂载幂等拉取 `GET /agents`（经 agentStore，失败降级，不阻塞渲染）。
 * - 打开 ProviderSettingsDialog 时拉取 `GET /providers`；保存/删除/导入成功后由
 *   ProviderSettingsDialog 内部触发 taskStore.refreshAvailableModels（关闭本身不刷新）。
 *
 * @example
 * ```tsx
 * // NewTaskPage / ChatPanel 顶部（默认自治）
 * <TaskHeaderBar className="self-end" />
 * // 受控模式（响应 guardSend 联动）
 * <TaskHeaderBar settingsOpen={settingsOpen} onSettingsOpenChange={setSettingsOpen} />
 * ```
 */
export function TaskHeaderBar({ className, settingsOpen: controlledOpen, onSettingsOpenChange }: TaskHeaderBarProps) {
  // ProviderSettingsDialog 受控开关：默认仅由 ModelSelector 的 footer / 空态入口触发，
  // 不下沉到 store（同上注释）。未传受控 props 时使用局部状态自治；传了则由宿主接管。
  const [localSettingsOpen, setLocalSettingsOpen] = useState(false);
  const settingsOpen = controlledOpen ?? localSettingsOpen;

  /**
   * 统一开关写入：受控模式下转发给宿主的 onSettingsOpenChange，否则写局部状态。
   *
   * @param next - 目标开关状态：true 为打开，false 为关闭。
   * @returns 无返回值（void）。
   */
  const setSettingsOpen = (next: boolean) => {
    if (onSettingsOpenChange) {
      onSettingsOpenChange(next);
    } else {
      setLocalSettingsOpen(next);
    }
  };

  // 打开入口：经 INFO 级别日志记录「用户操作入口 + 关键状态变更」，
  // 便于排查「点配置无反应」工单时定位到 TaskHeaderBar 调用链。
  /**
   * 打开厂商配置中心对话框。
   *
   * 记录 INFO 级操作日志（module=TaskHeaderBar, action=open_settings）后置
   * settingsOpen 为 true，触发 ProviderSettingsDialog 受控渲染。
   *
   * @returns 无返回值（void）。
   */
  const handleOpenSettings = () => {
    logInfo("打开厂商配置中心", { module: "TaskHeaderBar", action: "open_settings" });
    setSettingsOpen(true);
  };
  // 关闭入口：受控对话框的 onOpenChange 既包含受控关闭也包含 ESC/外部点击等回退路径，
  // 此处统一记录「关闭」意图，便于复盘用户为何突然看不到对话框。
  /**
   * 处理对话框开关意图（受控 onOpenChange 回调）。
   *
   * 关闭路径（含 ESC / 外部点击等回退路径）记录 INFO 级操作日志
   * （module=TaskHeaderBar, action=close_settings），随后同步受控状态。
   *
   * @param next - 目标开关状态：true 为打开，false 为关闭。
   * @returns 无返回值（void）。
   */
  const handleSettingsOpenChange = (next: boolean) => {
    if (!next) {
      logInfo("关闭厂商配置中心", { module: "TaskHeaderBar", action: "close_settings" });
    }
    setSettingsOpen(next);
  };

  return (
    <div
      data-testid="task-header-bar"
      className={cn("flex h-8 items-center gap-0.5", className)}
    >
      {/* Agent 选择器：直接读 useTaskStore.selectedAgentId / setSelectedAgentId（去掉外部 props） */}
      <AgentSelector className="h-7" />

      {/* 模型选择器：走 useTaskStore.selectedModelName；onOpenSettings 由本组件持有的 settingsOpen 自治 */}
      <ModelSelector
        onOpenSettings={handleOpenSettings}
        className="h-7"
      />

      {/* 受控对话框：关闭时不需在本组件刷新缓存——ProviderSettingsDialog 内部已处理 */}
      <ProviderSettingsDialog open={settingsOpen} onOpenChange={handleSettingsOpenChange} />
    </div>
  );
}
