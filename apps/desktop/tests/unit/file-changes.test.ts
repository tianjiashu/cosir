import { describe, expect, it } from "vitest";
import { readFileChanges } from "@/lib/assistant/tools/file-changes";
import { aggregateChanges } from "@/lib/assistant/turn/turn-layout";
import { toolCallPart } from "./transport-fixtures";

describe("shared file operation projection", () => {
  it("preserves original operation identity and shares records across detail and aggregate", () => {
    const changes = [null, { path: "a.ts", status: "deleted" }, { path: "a.ts", status: "added", patch: "patch", insertions: 2 }];
    const records = readFileChanges(changes);
    const card = aggregateChanges([{ index: 3, part: toolCallPart("call", "apply_patch", { display_data: { kind: "file-changes", changes } }) }]);
    expect(records.map(change => change.index)).toEqual([1, 2]);
    expect(card.files).toHaveLength(1);
    expect(card.files[0].operations.map(op => op.change.status)).toEqual(["deleted", "added"]);
    expect(card.files[0].operations[1].change).toBe(records[1]);
    expect(readFileChanges(changes)).toBe(records);
    const replacement = [...changes];
    replacement[2] = { path: "a.ts", status: "added", patch: "new patch", insertions: 3 };
    expect(readFileChanges(replacement)[1].patch).toBe("new patch");
  });

  it("normalizes malformed display data once without collapsing repeated paths", () => {
    expect(readFileChanges({})).toEqual([]);
    const records = readFileChanges([false, {}, { path: 1 }, { path: "a", status: "unknown", insertions: -2, deletions: Infinity }, { path: "a", status: "moved", new_path: "b", insertions: 2.9 }]);
    expect(records).toMatchObject([
      { index: 3, status: "modified", insertions: 0, deletions: 0, new_path: null },
      { index: 4, status: "moved", new_path: "b", insertions: 2 },
    ]);
  });
});
