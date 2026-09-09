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

  it("uses the declared presentation when read_file has no semantic data", () => {
    expect(routeToolPart("read_file", { presentation: { expand_layout: "none" }, data: null })).toBe("details");
  });

  it("requires delete semantics for the delete renderer", () => {
    expect(routeToolPart("delete", { presentation: { expand_layout: "none" }, data: null })).toBe("delete");
  });

  it("uses an explicit safe fallback for unknown tools", () => {
    expect(routeToolPart("future_tool", { presentation: {}, data: null })).toBe("fallback");
  });

  it("routes web search to its semantic renderer", () => {
    expect(routeToolPart("web_search", {
      backendStatus: "completed",
      presentation: { expand_layout: "list" },
      data: { kind: "web-search-results", query: "assistant-ui", results: [] },
    })).toBe("web-search");
  });

  it("routes web extract to the status-only renderer", () => {
    expect(routeToolPart("web_extract", {
      backendStatus: "completed",
      presentation: { expand_layout: "none", expandable: false },
      data: {
        kind: "web-extract-status",
        sites: [{ site: "example.com", url: "https://example.com", status: "success" }],
      },
    })).toBe("web-extract-status");
  });
});
