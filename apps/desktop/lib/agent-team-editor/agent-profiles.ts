import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";
import type { AgentConfiguration } from "@/lib/api/configuration";

/** Team 编辑器按作用域可选的子 Agent：system 只看系统级；workspace 见 `visibleWorkspaceProfiles`。 */
export type AgentProfilesByScope = Record<AgentTeamConfiguration["scope"], AgentConfiguration[]>;

/** 工作区可见的子 Agent = 系统 + 工作区，同名（同 `agent_id`）时工作区覆盖系统。 */
export function visibleWorkspaceProfiles(system: AgentConfiguration[], workspace: AgentConfiguration[]): AgentConfiguration[] {
  return [...new Map([...system, ...workspace].map((profile) => [profile.agent_id, profile])).values()];
}
