import { describe, expect, it } from "vitest";

import { buildTeamElkGraph, computeTeamAutoLayout } from "@/components/agent-team-graph-layout";
import { createGraphEdges, createGraphNodes } from "@/components/agent-team-graph-model";
import { createIdentityAllocator, importDraft } from "@/lib/agent-team-editor/model";
import { teamConfiguration } from "./team-editor-fixture";

// fixture：reviewer（入口）done→END、needs_changes→fixer，fixer done→reviewer，构成回路。
const config = importDraft(teamConfiguration, createIdentityAllocator());
const [reviewer, fixer] = config.nodes;
const END = "@@canvas-end-1";
// 故意把 fixer 和 END 摆在入口左边，验证整理结果不受原布局误导。
const nodes = createGraphNodes(config, {
  [reviewer.editorId]: { x: 800, y: 0 },
  [fixer.editorId]: { x: 0, y: 400 },
  [END]: { x: -300, y: 0 },
}, [END]);
const edges = createGraphEdges(config, {}, [END]);

describe("Agent Team auto layout", () => {
  it("pins START first and END last, with status ports in on-node order", () => {
    const graph = buildTeamElkGraph(nodes, edges);
    const node = (id: string) => graph.children!.find((child) => child.id === id)!;

    expect(node(reviewer.editorId).layoutOptions).toMatchObject({ "elk.layered.layering.layerConstraint": "FIRST_SEPARATE", "elk.portConstraints": "FIXED_ORDER" });
    expect(node(END).layoutOptions).toMatchObject({ "elk.layered.layering.layerConstraint": "LAST_SEPARATE" });
    expect(node(reviewer.editorId).ports!.map((port) => [port.id, port.layoutOptions?.["elk.port.index"]])).toEqual([
      [`${reviewer.editorId}::${reviewer.statuses[0].editorId}`, "0"],
      [`${reviewer.editorId}::${reviewer.statuses[1].editorId}`, "1"],
    ]);
  });

  it("lays a looping team out left to right from START to END", async () => {
    const positions = await computeTeamAutoLayout(nodes, edges);

    expect(positions[reviewer.editorId].x).toBeLessThan(positions[fixer.editorId].x);
    expect(positions[fixer.editorId].x).toBeLessThan(positions[END].x);
    // 结果平移到原包围盒左上角，视口不会大幅跳动。
    expect(Math.min(...Object.values(positions).map((position) => position.x))).toBe(-300);
  });

  it("treats only edges back to earlier steps as loops, keeping the execution order", async () => {
    // plan → code → test → review → END，另有 test.fail 与 review.needs_fix 回到 code。
    // 指向 code 的边多于它的出边；按「反转边最少」拆环会把 code → test 当回头边，排乱顺序。
    const status = (name: string) => ({ name, color: "#3B82F6" });
    const pipeline = importDraft({
      ...teamConfiguration,
      start_node_id: "plan",
      nodes: [
        { node_id: "review", name: "审查", agent_id: "reviewer", statuses: [status("done"), status("needs_fix")] },
        { node_id: "code", name: "实现", agent_id: "coder", statuses: [status("done")] },
        { node_id: "plan", name: "规划", agent_id: "planner", statuses: [status("done"), status("blocked")] },
        { node_id: "test", name: "测试", agent_id: "tester", statuses: [status("pass"), status("fail")] },
      ],
      transitions: [
        { from_node_id: "plan", status: "done", target_node_id: "code" },
        { from_node_id: "plan", status: "blocked", target_node_id: "END" },
        { from_node_id: "code", status: "done", target_node_id: "test" },
        { from_node_id: "test", status: "pass", target_node_id: "review" },
        { from_node_id: "test", status: "fail", target_node_id: "code" },
        { from_node_id: "review", status: "done", target_node_id: "END" },
        { from_node_id: "review", status: "needs_fix", target_node_id: "code" },
      ],
    }, createIdentityAllocator());
    const pipelineNodes = createGraphNodes(pipeline, {}, [END]);
    const positions = await computeTeamAutoLayout(pipelineNodes, createGraphEdges(pipeline, {}, [END]));
    const x = (nodeId: string) => positions[pipeline.nodes.find((node) => node.node_id === nodeId)!.editorId].x;

    expect([x("plan"), x("code"), x("test"), x("review"), positions[END].x]).toEqual(
      [x("plan"), x("code"), x("test"), x("review"), positions[END].x].toSorted((a, b) => a - b),
    );
    expect(new Set([x("plan"), x("code"), x("test"), x("review"), positions[END].x]).size).toBe(5);
  });
});
