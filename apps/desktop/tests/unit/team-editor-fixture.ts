import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";
export const teamConfiguration: AgentTeamConfiguration = {
  team_id: "review", name: "Review", description: "Review changes", max_runs: 8, start_node_id: "reviewer", scope: "workspace",
  nodes: [
    { node_id: "reviewer", name: "Review", agent_id: "reviewer_agent", statuses: [{ name: "done", color: "#16A34A" }, { name: "needs_changes", color: "#F59E0B" }] },
    { node_id: "fixer", name: "Fix", agent_id: "fixer_agent", statuses: [{ name: "done", color: "#16A34A" }] },
  ],
  transitions: [
    { from_node_id: "reviewer", status: "done", target_node_id: "END" },
    { from_node_id: "reviewer", status: "needs_changes", target_node_id: "fixer" },
    { from_node_id: "fixer", status: "done", target_node_id: "reviewer" },
  ],
};
