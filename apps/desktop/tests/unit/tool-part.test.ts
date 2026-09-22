import { describe, expect, it } from "vitest";

import { routeToolPart } from "@/components/assistant-ui/tools/tool-part";
import { safeExternalUrl } from "@/components/assistant-ui/tools/types";

describe("tool renderer routing", () => {
  it("routes an explicit read_file data kind to details", () => {
    expect(routeToolPart("read_file", {
      backendStatus: "completed",
      presentation: { expand_layout: "none", expandable: false },
      display_data: { kind: "read-file-meta", path: "README.md" },
    })).toBe("details");
  });

  it("uses the declared presentation when read_file has no semantic data", () => {
    expect(routeToolPart("read_file", { presentation: { expand_layout: "none" }, display_data: null })).toBe("details");
  });

  it("uses an explicit safe fallback for unknown tools", () => {
    expect(routeToolPart("future_tool", { presentation: {}, display_data: null })).toBe("fallback");
  });

  it("does not let an unknown display kind bypass the fallback", () => {
    expect(routeToolPart("future_tool", {
      presentation: { expand_layout: "none", verb: "未来工具" },
      display_data: { kind: "future-display-kind" },
    })).toBe("fallback");
  });

  it("routes generic list data through the details renderer", () => {
    expect(routeToolPart("web_search", {
      backendStatus: "completed",
      presentation: { expand_layout: "list" },
      display_data: { kind: "web-search-results", results: [{ title: "result", url: "https://example.com" }] },
    })).toBe("details");
  });

  it("routes delegation refs to the dedicated activity row", () => {
    expect(routeToolPart("delegate_task", {
      backendStatus: "running",
      presentation: { surface: "standalone", expand_layout: "none" },
      display_data: { kind: "delegation-result", title: "审查代码", role: "Reviewer", child_task_id: 501, child_run_id: 902 },
      child_task_id: 501,
      child_run_id: 902,
      agent_role: "Reviewer",
    })).toBe("delegation");
  });

  it("routes child_agent_wait results by stable display kind", () => {
    expect(routeToolPart("child_agent_wait", {
      backendStatus: "completed",
      presentation: { expand_layout: "details" },
      display_data: {
        kind: "child-agent-wait-result",
        timed_out: false,
        messages: [],
        pending: [],
        interrupted_by: null,
      },
    })).toBe("details");
  });

  it("sends malformed display data to the fallback without trusting presentation", () => {
    expect(routeToolPart("child_agent_wait", {
      backendStatus: "completed",
      presentation: { expand_layout: "details", verb: "等待子 Agent" },
      display_data: { kind: 42, messages: "not-an-array" },
    })).toBe("fallback");
  });

  it("sends malformed delegation payloads to the fallback", () => {
    expect(routeToolPart("delegate_task", {
      backendStatus: "completed",
      presentation: { expand_layout: "none" },
      display_data: { kind: "delegation-result", title: "子 Agent", child_task_id: 0 },
    })).toBe("fallback");
  });

  it("routes interactive terminal sessions to the task-scoped readonly panel", () => {
    expect(routeToolPart("terminal_start", {
      backendStatus: "completed",
      presentation: { variant: "terminal-session-start", expand_layout: "terminal" },
      display_data: { kind: "terminal-session", session_id: "term_demo" },
    })).toBe("terminal-session");
  });

  it("allows only http(s) URLs from tool display data", () => {
    expect(safeExternalUrl("https://example.com/a")).toBe("https://example.com/a");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("data:text/html,<script>alert(1)</script>")).toBeNull();
    expect(safeExternalUrl("https://user:pass@example.com/a")).toBeNull();
    expect(safeExternalUrl("http://127.0.0.1:8000/a")).toBeNull();
    expect(safeExternalUrl("http://192.168.1.20/a")).toBeNull();
  });
});
