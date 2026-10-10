import { describe, expect, it } from "vitest";

import {
  commitGraphEdit,
  createGraphEditHistory,
  redoGraphEdit,
  undoGraphEdit,
  type AgentTeamGraphSnapshot,
} from "@/components/agent-team-graph-history";
import type { AgentTeamConfiguration } from "@/lib/api/agent-teams";

const configuration: AgentTeamConfiguration = {
  team_id: "review",
  name: "Review",
  description: "",
  max_runs: 3,
  start_node_id: "reviewer",
  scope: "workspace",
  nodes: [{ node_id: "reviewer", name: "Review", agent_id: "reviewer", statuses: ["done"] }],
  transitions: [{ from_node_id: "reviewer", status: "done", target_node_id: "END" }],
};

function snapshot(name: string): AgentTeamGraphSnapshot {
  return {
    configuration: { ...configuration, name },
    layout: { positions: { reviewer: { x: name.length, y: 0 } }, endNodeIds: ["@@canvas-end-1"], endTargets: {} },
  };
}

describe("Agent Team graph edit history", () => {
  it("undoes and redoes a complete configuration and layout snapshot", () => {
    const initial = snapshot("Review");
    const edited = snapshot("Edited");
    const afterEdit = commitGraphEdit(createGraphEditHistory(), initial, edited, { now: 1 });

    const undo = undoGraphEdit(afterEdit, edited);
    expect(undo?.snapshot).toEqual(initial);
    const redo = redoGraphEdit(undo!.history, initial);
    expect(redo?.snapshot).toEqual(edited);
  });

  it("coalesces nearby changes with the same key into one undo step", () => {
    const first = snapshot("A");
    const second = snapshot("AB");
    const third = snapshot("ABC");
    const history = commitGraphEdit(
      commitGraphEdit(createGraphEditHistory(), first, second, { coalesceKey: "name", now: 10 }),
      second,
      third,
      { coalesceKey: "name", now: 20 },
    );

    expect(undoGraphEdit(history, third)?.snapshot).toEqual(first);
  });

  it("clears the redo branch after a new edit", () => {
    const first = snapshot("A");
    const second = snapshot("B");
    const third = snapshot("C");
    const history = commitGraphEdit(createGraphEditHistory(), first, second, { now: 1 });
    const undone = undoGraphEdit(history, second)!;
    const branched = commitGraphEdit(undone.history, first, third, { now: 2 });

    expect(redoGraphEdit(branched, third)).toBeNull();
  });

  it("bounds history so long editing sessions do not retain every snapshot", () => {
    let history = createGraphEditHistory();
    let current = snapshot("0");
    for (let index = 1; index <= 60; index += 1) {
      const next = snapshot(String(index));
      history = commitGraphEdit(history, current, next, { now: index * 1000 });
      current = next;
    }

    expect(history.past).toHaveLength(50);
  });
});
