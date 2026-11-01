import { describe, expect, it } from "vitest";

import { routeToolPart } from "@/components/assistant-ui/tools/tool-part";
import { safeExternalUrl } from "@/components/assistant-ui/tools/types";

describe("tool renderer routing", () => {
  it("routes an explicit read_file data kind to details", () => {
    expect(routeToolPart({
      backendStatus: "completed",
      presentation: { expand_layout: "none", expandable: false },
      display_data: { kind: "read-file-meta", path: "README.md" },
    })).toBe("details");
  });

  it("uses the declared presentation when read_file has no semantic data", () => {
    expect(routeToolPart({ presentation: { expand_layout: "none" }, display_data: null })).toBe("details");
  });

  it("uses an explicit safe fallback for unknown tools", () => {
    expect(routeToolPart({ presentation: {}, display_data: null })).toBe("fallback");
  });

  it("does not let an unknown display kind bypass the fallback", () => {
    expect(routeToolPart({
      presentation: { expand_layout: "none", verb: "未来工具" },
      display_data: { kind: "future-display-kind" },
    })).toBe("fallback");
  });

  it("routes generic list data through the details renderer", () => {
    expect(routeToolPart({
      backendStatus: "completed",
      presentation: { expand_layout: "list" },
      display_data: { kind: "web-search-results", results: [{ title: "result", url: "https://example.com" }] },
    })).toBe("details");
  });

  it("routes MCP and Skill results through the details renderer", () => {
    expect(routeToolPart({
      presentation: { expand_layout: "details" },
      display_data: { kind: "mcp-tool-result", action: "list", servers: [] },
    })).toBe("details");
    expect(routeToolPart({
      presentation: { expand_layout: "details" },
      display_data: { kind: "skill-content", action: "list", skills: [] },
    })).toBe("details");
  });

  it("routes delegation refs to the dedicated activity row", () => {
    expect(routeToolPart({
      backendStatus: "running",
      presentation: { surface: "standalone", expand_layout: "none" },
      display_data: { kind: "delegation-result", title: "审查代码", role: "Reviewer", child_task_id: 501 },
      child_task_id: 501,
      agent_role: "Reviewer",
    })).toBe("delegation");
  });

  it("keeps a failed TeamRun on the execution card after its display data becomes a short hint", () => {
    expect(routeToolPart({
      backendStatus: "failed",
      presentation: { expand_layout: "details" },
      display_data: { status_hint: "执行失败" },
      team_run_id: 17,
    })).toBe("agent-team-execution");
  });

  it("routes wait_messages, which has no display data, to the generic fallback card", () => {
    // 挂起只是控制流；卡片只需动词与状态，不需要专用 kind。
    expect(routeToolPart({
      backendStatus: "completed",
      presentation: { expand_layout: "none", verb: "等待结果" },
      display_data: {},
    })).toBe("fallback");
  });

  it("sends malformed display data to the fallback without trusting presentation", () => {
    expect(routeToolPart({
      backendStatus: "completed",
      presentation: { expand_layout: "details", verb: "读取子 Agent 状态" },
      display_data: { kind: 42, messages: "not-an-array" },
    })).toBe("fallback");
  });

  it("sends malformed delegation payloads to the fallback", () => {
    expect(routeToolPart({
      backendStatus: "completed",
      presentation: { expand_layout: "none" },
      display_data: { kind: "delegation-result", title: "子 Agent", child_task_id: 0 },
    })).toBe("fallback");
  });

  it("routes interactive terminal sessions to the task-scoped readonly panel", () => {
    expect(routeToolPart({
      backendStatus: "completed",
      presentation: { variant: "terminal-session-start", expand_layout: "terminal" },
      display_data: { kind: "terminal-session", session_id: "term_demo" },
    })).toBe("terminal-session");
  });

  // 目的：会话工具在还没有 kind 的阶段（排队、运行中、失败）也走会话卡。潜在缺陷：只按布局兜底时，
  // terminal_start（布局为 terminal）落进一次性命令卡、显示停止按钮，其余会话工具落进通用行，完成时外形突变。
  it.each([
    ["terminal_start 运行中", "running", { variant: "terminal-session-start", expand_layout: "terminal" }, null],
    ["terminal_start 失败", "failed", { variant: "terminal-session-start", expand_layout: "terminal" }, { status_hint: "启动失败" }],
    ["terminal_read 运行中", "running", { variant: "terminal-session-read", expand_layout: "none", verb: "读取终端" }, null],
  ])("routes %s without a kind to the session card", (_label, backendStatus, presentation, displayData) => {
    expect(routeToolPart({ backendStatus, presentation, display_data: displayData })).toBe("terminal-session");
  });

  it("keeps one-off commands without a kind on the terminal command card", () => {
    expect(routeToolPart({
      backendStatus: "running",
      presentation: { verb: "执行命令", expand_layout: "terminal" },
      display_data: null,
    })).toBe("terminal");
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
