import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useStore } from "zustand";

import { checkAgentTeamGraph, type AgentTeamGraphIssue } from "@/lib/api/agent-teams";
import type { TeamEditorSession } from "@/lib/agent-team-editor/session";
import { queryKeys } from "@/lib/query/query-keys";

/** 停止编辑多久后再检查；连续输入只发最后一次。 */
const CHECK_DELAY_MS = 400;

export type TeamGraphIssues = {
  issues: AgentTeamGraphIssue[];
  /** 检查请求失败（如后端不可用）；此时不展示上一次结果之外的结论。 */
  failed: boolean;
  byNodeId: Map<string, AgentTeamGraphIssue[]>;
  byTransitionIndex: Map<number, AgentTeamGraphIssue[]>;
  /** 不落在某个节点或转移上的 Team 级问题（无节点、入口无效等）。 */
  teamLevel: AgentTeamGraphIssue[];
};

/**
 * 编辑中 Team 草稿的图结构问题。规则只在后端维护（与保存校验同源），前端不重写。
 *
 * 只把参与图校验的字段（入口、节点 ID 与状态名、转移）作为查询键，改名称、颜色或
 * 布局不触发检查；输入停顿后才请求，结果切换时保留上一次结果，标注不会闪烁。
 */
export function useTeamGraphIssues(session: TeamEditorSession): TeamGraphIssues {
  const configuration = useStore(session.store, (state) => state.snapshot.configuration);
  const graph = useMemo(() => JSON.stringify({
    start_node_id: configuration.start_node_id,
    nodes: configuration.nodes.map((node) => ({ node_id: node.node_id, statuses: node.statuses.map(({ name }) => ({ name })) })),
    transitions: configuration.transitions.map(({ from_node_id, status, target_node_id }) => ({ from_node_id, status, target_node_id })),
  }), [configuration.nodes, configuration.start_node_id, configuration.transitions]);
  const [settledGraph, setSettledGraph] = useState(graph);

  useEffect(() => {
    const timeout = window.setTimeout(() => setSettledGraph(graph), CHECK_DELAY_MS);
    return () => window.clearTimeout(timeout);
  }, [graph]);

  const query = useQuery({
    queryKey: queryKeys.agentTeamGraphIssues(settledGraph),
    queryFn: ({ signal }) => checkAgentTeamGraph(JSON.parse(settledGraph), { signal }),
    placeholderData: keepPreviousData,
    staleTime: Infinity,
  });

  return useMemo(() => {
    const issues = query.data ?? [];
    const byNodeId = new Map<string, AgentTeamGraphIssue[]>();
    const byTransitionIndex = new Map<number, AgentTeamGraphIssue[]>();
    const teamLevel: AgentTeamGraphIssue[] = [];
    for (const issue of issues) {
      if (issue.transition_index !== null) byTransitionIndex.set(issue.transition_index, [...(byTransitionIndex.get(issue.transition_index) ?? []), issue]);
      if (issue.node_id !== null) byNodeId.set(issue.node_id, [...(byNodeId.get(issue.node_id) ?? []), issue]);
      if (issue.transition_index === null && issue.node_id === null) teamLevel.push(issue);
    }
    return { issues, failed: query.isError, byNodeId, byTransitionIndex, teamLevel };
  }, [query.data, query.isError]);
}
