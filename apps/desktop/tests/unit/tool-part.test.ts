import { describe, expect, it } from "vitest";

import { routeToolPart } from "@/components/assistant-ui/tools/tool-part";

describe("tool renderer routing", () => {
  it("routes an explicit read_file data kind to details", () => {
    expect(routeToolPart("read_file", {
      backendStatus: "completed",
      presentation: { expand_layout: "none", expandable: false },
      data: { kind: "read-file-meta", path: "README.md" },
    })).toBe("details");
  });

  it("does not guess a renderer when read_file has no semantic data", () => {
    expect(routeToolPart("read_file", { presentation: { expand_layout: "none" }, data: null })).toBe("unknown");
  });

  it("requires delete semantics for the delete renderer", () => {
    expect(routeToolPart("delete", { presentation: { expand_layout: "none" }, data: null })).toBe("delete");
  });

  it("uses an explicit safe fallback for unknown tools", () => {
    expect(routeToolPart("future_tool", { presentation: {}, data: null })).toBe("unknown");
  });
});
