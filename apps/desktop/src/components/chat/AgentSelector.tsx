/**
 * Agent 选择器组件。
 *
 * 紧凑的下拉选择器，展示当前选中的 Agent 名称 + chevron；
 * 点击后弹出 Agent 列表供切换。数据来自后端 GET /agents API。
 *
 * 样式参考 Codex 桌面端的模型选择器：折叠态为单行 `图标 名 ∨`，
 * 展开态为带圆角的浮层列表，每项含名称与角色描述。
 *
 * @module components/chat/AgentSelector
 */

import { useEffect, useState, useCallback } from "react";
import { ChevronDown, Check, Bot } from "lucide-react";
import { cn } from "@/lib/utils";
import { logError } from "@/lib/logger";
import { listAgents } from "@/services/api";
import type { AgentProfileResponse, ListAgentsResponse } from "@shared/api";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Caption } from "@/components/ui/tokens";

/** 默认 Agent ID（与后端 DEFAULT_AGENT_ID 对齐）。 */
const FALLBACK_AGENT_ID = "developer";

/** AgentSelector 组件属性。 */
interface AgentSelectorProps {
  /** 当前选中的 agent_id。 */
  value: string;
  /** 切换 agent 时的回调。 */
  onChange: (agentId: string) => void;
  /** 可选的额外 CSS 类名。 */
  className?: string;
}

/**
 * Agent 下拉选择器。
 *
 * 首次挂载时从 /agents 拉取列表，后续由父组件通过 value/onChange 控制选中态。
 * 网络失败时降级为仅显示默认 developer，不阻塞输入区渲染。
 */
export function AgentSelector({ value, onChange, className }: AgentSelectorProps) {
  const [agents, setAgents] = useState<AgentProfileResponse[]>([]);
  const [defaultId] = useState(FALLBACK_AGENT_ID);
  const [open, setOpen] = useState(false);

  /** 从后端拉取已注册 Agent 列表。 */
  const fetchAgents = useCallback(async () => {
    try {
      const response: ListAgentsResponse = await listAgents();
      setAgents(response.agents);
    } catch (err) {
      // 降级：列表为空时 UI 仅显示 fallback，不阻塞输入；失败仍经统一出口记录，便于排查后端/网络问题
      logError("拉取 Agent 列表失败，降级为 fallback", err, { module: "AgentSelector" });
      setAgents([]);
    }
  }, []);

  // 首次挂载拉取一次（Agent 列表在会话生命周期内不变）
  useEffect(() => {
    void fetchAgents();
  }, [fetchAgents]);

  /** 当前选中项的显示名称（优先用 role，回退到 agent_id）。 */
  const currentLabel =
    agents.find((a) => a.agent_id === value)?.role ??
    (value === FALLBACK_AGENT_ID ? "Developer" : value);

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
          /* 加载中或网络失败时的降级展示 */
          <div className="px-2 py-1.5 text-xs text-muted-foreground">
            {agents.length === 0 && value === FALLBACK_AGENT_ID ? "加载中..." : "无可用 Agent"}
          </div>
        ) : (
          agents.map((agent) => {
            const isSelected = agent.agent_id === value || (!value && agent.agent_id === defaultId);
            return (
              <button
                key={agent.agent_id}
                type="button"
                onClick={() => {
                  onChange(agent.agent_id);
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
