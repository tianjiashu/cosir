import { indexNodeIds } from "@/lib/agent-team-editor/node-id-index";
import { describe, expect, it } from "vitest";
import { buildTeamOutline } from "@/lib/agent-team-editor/outline";
import { createIdentityAllocator, importDraft } from "@/lib/agent-team-editor/model";
import { teamConfiguration } from "./team-editor-fixture";

describe("Team 表单大纲", () => {
  it("按入口广度优先排列，循环不重复，END 不生成节点", () => {
    const draft = importDraft(teamConfiguration, createIdentityAllocator());
    expect(buildTeamOutline(draft, indexNodeIds(draft.nodes))).toEqual({ connected: [0, 1], unconnected: [], dangling: [] });
  });

  it.each(["missing", "duplicate"])("保留 %s 来源转移，不把歧义入口分配给任一节点", (kind) => {
    const draft = importDraft({ ...teamConfiguration,
      nodes: kind === "missing" ? [teamConfiguration.nodes[1]] : [teamConfiguration.nodes[0], teamConfiguration.nodes[0], teamConfiguration.nodes[1]],
    }, createIdentityAllocator());
    expect(buildTeamOutline(draft, indexNodeIds(draft.nodes))).toEqual({
      connected: [], unconnected: kind === "missing" ? [0] : [0, 1, 2], dangling: [0, 1],
    });
  });

  it("空 ID 也按唯一性解析，保留编辑中草稿", () => {
    const draft = importDraft({ ...teamConfiguration, start_node_id: "",
      nodes: [{ ...teamConfiguration.nodes[0], node_id: "" }, teamConfiguration.nodes[1]],
      transitions: [{ from_node_id: "", status: "done", target_node_id: "fixer" }],
    }, createIdentityAllocator());
    expect(buildTeamOutline(draft, indexNodeIds(draft.nodes))).toEqual({ connected: [0, 1], unconnected: [], dangling: [] });
  });
});
