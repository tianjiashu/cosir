import { describe, expect, it } from "vitest";

import { routeToolPart } from "@/components/assistant-ui/tools/tool-part";
import { safeExternalUrl } from "@/components/assistant-ui/tools/types";

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

  it("routes generic list data through the details renderer", () => {
    expect(routeToolPart("web_search", {
      backendStatus: "completed",
      presentation: { expand_layout: "list" },
      data: { kind: "web-search-results", results: [{ title: "result", url: "https://example.com" }] },
    })).toBe("details");
  });

  it("allows only http(s) URLs from tool display data", () => {
    expect(safeExternalUrl("https://example.com/a")).toBe("https://example.com/a");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
  });
});
