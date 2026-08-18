/**
 * Agent 选择器组件。
 *
 * 紧凑的下拉选择器，展示当前选中的 Agent 名称 + chevron；
 * 点击后弹出 Agent 列表供切换。列表数据来自 `stores/agentStore`——Agent 列表的
 * 单一数据源，与发送前模型校验（useModelSendGuard）共享同一份缓存，避免各自
 * 拉取造成双请求与数据漂移。
 *
 * 状态来源切换：直接读 `useTaskStore.selectedAgentId` / `setSelectedAgentId`，
 * 不再接受 `value / onChange` props——与 ModelSelector 保持同一事实源
 * 写入约定，避免不同调用方各自传 props 导致「显示与 store 撕裂」。
 *
 * 样式参考 Codex 桌面端的模型选择器：折叠态为单行 `图标 名 ∨`，
 * 展开态为带圆角的浮层列表，每项含名称与角色描述。
 *
 * @module components/chat/AgentSelector
 */

import { useEffect, useState } from "react";
import { ChevronDown, Check, Bot } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAgentStore } from "@/stores/agentStore";
import { useTaskStore } from "@/stores/taskStore";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Caption } from "@/components/ui/tokens";

/** AgentSelector 组件属性。 */
interface AgentSelectorProps {
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * Agent 下拉选择器。
 *
 * 列表数据消费 `agentStore.agents`（与 useModelSendGuard 共享单一缓存）；首次
 * 挂载时经 `agentStore.refreshAgents()` 幂等拉取一次（已加载成功则直接跳过，
 * 多个 TaskHeaderBar 实例并存时也只会发一次 `/agents` 请求）。当前选中态与变更
 * 回调均直接经 useTaskStore 写入，与 ModelSelector 保持一致
 * （selectedModelName / setSelectedModelName）。
 * 网络失败时 store 保留空缓存（defaultAgentId 回退 "developer"），折叠态
 * 降级为仅显示默认 Agent，不阻塞输入区渲染；展开态按 store.loaded 区分
 * 「拉取中/失败重试前」与「已加载但列表为空」（分别显示 加载中… / 无可用 Agent）。
 *
 * @param props - 组件属性。
 * @param props.className - 可选的外层类名注入。
 * @returns AgentSelector 的 React 元素。
 *
 * @sideeffect
 * - 首次挂载时调用 `agentStore.refreshAgents()` 幂等拉取 `GET /agents`；
 *   失败由 agentStore 记录 error 日志并保留空缓存（本组件降级展示）。
 *
 * @example
 * ```tsx
 * // 任意 task 维度选择场景：直接读 store
 * <AgentSelector className="h-7" />
 * ```
 */
export function AgentSelector({ className }: AgentSelectorProps) {
  const [open, setOpen] = useState(false);

  // 直接读 store：与 ModelSelector 同一事实源
  const value = useTaskStore((s) => s.selectedAgentId);
  const setValue = useTaskStore((s) => s.setSelectedAgentId);

  // Agent 列表来自 agentStore（单一数据源，与 useModelSendGuard 共享缓存）。
  const agents = useAgentStore((s) => s.agents);
  const defaultAgentId = useAgentStore((s) => s.defaultAgentId);
  const loaded = useAgentStore((s) => s.loaded);

  // 首次挂载幂等拉取一次（Agent 列表在会话生命周期内不变；已加载时直接跳过）。
  useEffect(() => {
    void useAgentStore.getState().refreshAgents();
  }, []);

  /** 当前选中项的显示名称（优先用 role，回退到 agent_id）。 */
  const currentLabel =
    agents.find((a) => a.agent_id === value)?.role ??
    (value === defaultAgentId ? "Developer" : value);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex cursor-pointer items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium text-muted-foreground hover:bg-accent transition-colors",
            className,
          )}
        >
          <Bot className="h-3.5 w-3.5 shrink-0" />
          {/* 选择器宽度 80px 是紧凑折叠态的固定净空，非通用语义，待收敛到 token */}
          {/* eslint-disable-next-line tailwind/no-arbitrary-value */}
          <span className="max-w-[80px] truncate">{currentLabel}</span>
          <ChevronDown className="h-3 w-3 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>

      <PopoverContent align="start" className="w-64 p-1">
        {agents.length === 0 ? (
          /* 未加载完成（拉取中/失败重试前）显示加载中；已加载但列表为空显示无可用 */
          <div className="px-2 py-1.5 text-xs text-muted-foreground">
            {loaded ? "无可用 Agent" : "加载中..."}
          </div>
        ) : (
          agents.map((agent) => {
            const isSelected = agent.agent_id === value || (!value && agent.agent_id === defaultAgentId);
            return (
              <button
                key={agent.agent_id}
                type="button"
                onClick={() => {
                  setValue(agent.agent_id);
                  setOpen(false);
                }}
                className={cn(
                  "flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-xs transition-colors",
                  "hover:bg-accent hover:text-accent-foreground",
                  isSelected && "bg-accent text-accent-foreground",
                )}
              >
                <Bot className="h-4 w-4 shrink-0 text-muted-foreground" />
                <div className="flex flex-1 items-center justify-between gap-2 overflow-hidden">
                  <div className="min-w-0">
                    <div className="truncate font-medium">{agent.role}</div>
                    <div className={cn("truncate text-muted-foreground", Caption.xs)}>
                      {agent.model_name}
                    </div>
                  </div>
                  {isSelected && <Check className="h-3.5 w-3.5 shrink-0 text-primary" />}
                </div>
              </button>
            );
          })
        )}
      </PopoverContent>
    </Popover>
  );
}
