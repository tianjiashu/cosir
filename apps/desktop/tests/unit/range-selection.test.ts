import { describe, expect, it } from "vitest";

import { toggleSelection } from "@/lib/selection/range-selection";

const visible = ["a", "b", "c", "d", "e"];

describe("toggleSelection", () => {
  it("plain click toggles only the target", () => {
    expect([...toggleSelection(visible, new Set(["a"]), "c", { anchor: "a", range: false })]).toEqual(["a", "c"]);
    expect([...toggleSelection(visible, new Set(["a", "c"]), "c", { anchor: "a", range: false })]).toEqual(["a"]);
  });

  it("shift click sets the whole span to the target's new state, in either direction", () => {
    expect(toggleSelection(visible, new Set(["b"]), "d", { anchor: "b", range: true })).toEqual(new Set(["b", "c", "d"]));
    expect(toggleSelection(visible, new Set(), "b", { anchor: "e", range: true })).toEqual(new Set(["b", "c", "d", "e"]));
    expect(toggleSelection(visible, new Set(visible), "d", { anchor: "b", range: true })).toEqual(new Set(["a", "e"]));
  });

  it("falls back to a plain toggle when the anchor is filtered out or missing", () => {
    expect(toggleSelection(["c", "d"], new Set(["x"]), "d", { anchor: "a", range: true })).toEqual(new Set(["x", "d"]));
    expect(toggleSelection(visible, new Set(), "c", { anchor: null, range: true })).toEqual(new Set(["c"]));
  });
});
