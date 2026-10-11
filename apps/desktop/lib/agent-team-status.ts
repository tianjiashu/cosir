import type { CSSProperties } from "react";

import type { AgentTeamStatusConfiguration } from "@/lib/api/agent-teams";

export const DEFAULT_AGENT_TEAM_STATUS_COLOR = "#3B82F6";

export function findAgentTeamStatus(
  statuses: AgentTeamStatusConfiguration[],
  name: string,
): AgentTeamStatusConfiguration | undefined {
  return statuses.find((status) => status.name === name);
}

export function statusColorStyle(color: string): CSSProperties {
  return {
    borderColor: color,
    backgroundColor: `color-mix(in srgb, ${color} 10%, transparent)`,
  };
}
