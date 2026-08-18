/**
 * Agent profile 列表状态管理（Zustand）。
 *
 * 作为 Agent 列表的单一数据源：AgentSelector（下拉展示）与发送前模型校验
 * （useModelSendGuard）共享同一份缓存，避免各自拉取造成双请求与数据漂移。
 *
 * Agent 列表在会话生命周期内不变（后端静态注册），故只在首次需要时拉取一次；
 * 拉取失败保留降级语义（agents 为空，调用方各自处理 fallback）。
 *
 * @module stores/agentStore
 */

import { create } from "zustand";
import type { AgentProfileResponse } from "@shared/api";
import { listAgents } from "@/services/api";
import { logError } from "@/lib/logger";

/** Agent Store 的状态接口。 */
interface AgentState {
  /** 已注册的 Agent profile 列表（空数组 = 未加载或加载失败）。 */
  agents: AgentProfileResponse[];
  /** 默认 Agent 标识（与后端 DEFAULT_AGENT_ID 对齐；加载失败回退 "developer"）。 */
  defaultAgentId: string;
  /** 是否已成功从后端拉取过（失败重试依据）。 */
  loaded: boolean;
}

/** Agent Store 的动作接口。 */
interface AgentActions {
  /**
   * 从后端拉取 Agent 列表（幂等：已加载成功时跳过，force 可强制重拉）。
   *
   * @param force - 为 true 时忽略已加载态强制重拉（网络恢复后重试用）。
   * @returns 拉取成功返回 true；失败返回 false（保留旧缓存，允许下次重试）。
   */
  refreshAgents: (force?: boolean) => Promise<boolean>;
}

/**
 * Agent Zustand Store 实例。
 *
 * 使用 create 支持组件外直接调用（如校验 hook 在非渲染上下文读取）。
 */
export const useAgentStore = create<AgentState & AgentActions>((set, get) => ({
  agents: [],
  defaultAgentId: "developer",
  loaded: false,

  refreshAgents: async (force = false) => {
    if (get().loaded && !force) {
      return true;
    }
    try {
      const response = await listAgents();
      set({ agents: response.agents, defaultAgentId: response.default_agent_id, loaded: true });
      return true;
    } catch (err) {
      // 失败保留旧缓存（通常为空）：AgentSelector 降级显示 fallback。
      // 模型发送校验仅依赖 taskStore 的 availableModels 缓存，与 Agent 列表无关，
      // 故此处失败不阻塞发送链路（2026-08-18 起模型必须显式选择，无默认模型回退）。
      logError("拉取 Agent 列表失败", err, { module: "agentStore" });
      return false;
    }
  },
}));

// ---------- 派生选择器 ----------

/**
 * 按 agent_id 查找 Agent profile。
 *
 * @param state - agentStore 状态。
 * @param agentId - 目标 Agent 标识。
 * @returns 匹配的 profile；未加载或未找到返回 undefined。
 */
export const selectAgentProfileById = (
  state: AgentState,
  agentId: string,
): AgentProfileResponse | undefined => {
  return state.agents.find((agent) => agent.agent_id === agentId);
};
