import { describe, expect, it } from "vitest";

import { TOOL_CATALOG, toolHeading, toolIconName, toolVerb } from "@/lib/assistant/tools/tool-catalog";

describe("toolHeading", () => {
  // 目的：排队时参数为空对象，取值必须容忍缺字段；潜在缺陷：拼出 "undefined in undefined" 之类的目标。
  it("参数还没到时只有动词、没有目标", () => {
    for (const toolName of Object.keys(TOOL_CATALOG)) {
      const heading = toolHeading(toolName, {});
      expect(heading.title, toolName).toBe(TOOL_CATALOG[toolName].verb);
      expect(heading.target, toolName).toBeNull();
    }
  });

  it("命令与委派用模型给的意图作标题，其余用动词", () => {
    expect(toolHeading("execute_terminal", { command: "pytest -q\necho" }, "Run backend unit tests"))
      .toEqual({ title: "Run backend unit tests", target: null });
    expect(toolHeading("delegate_task", { agent_name: "Review", child_agent_id: "reviewer" }))
      .toEqual({ title: "Review", target: "reviewer" });
    expect(toolHeading("read_file", { path: "app/main.py" })).toEqual({ title: "读取文件", target: "app/main.py" });
  });

  it("多目标只写首个加 +N，组合目标各段齐全才拼", () => {
    expect(toolHeading("search_content", { pattern: "ToolHeading", path: "app" }).target).toBe('"ToolHeading" in app');
    expect(toolHeading("search_content", { pattern: "x" }).target).toBeNull();
    expect(toolHeading("move_file", { source_path: "a.txt", destination_path: "b.txt" }).target).toBe("a.txt → b.txt");
    expect(toolHeading("read_image", { paths: ["a.png", "b.png"] }).target).toBe("a.png +1");
    expect(toolHeading("web_extract", { urls: ["https://example.com/a", "https://example.com/b"] }).target).toBe("https://example.com/a +1");
    expect(toolHeading("apply_patch", { patch: "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\ndiff --git a/x.py b/x.py\n" }).target).toBe("x.py");
    expect(toolHeading("browser_act", { actions: [{ type: "click", ref: "e1" }, { type: "scroll" }] }).target).toBe("点击 +1");
    expect(toolHeading("browser_logs", { kind: "network", failed_only: true }).target).toBe("network");
    expect(toolHeading("browser_evaluate", { expression: `document.title\n// ${"x".repeat(80)}` }).target).toBe("document.title");
    expect(toolHeading("browser_evaluate", { expression: "a".repeat(70) }).target).toBe(`${"a".repeat(60)}…`);
  });

  it("终端工具按 terminal_id 拼目标，MCP 与 Skill 按动作拼目标", () => {
    expect(toolHeading("terminal_write", { terminal_id: "term_1", command: "npm test" }).target).toBe("term_1 · npm test");
    expect(toolHeading("terminal_write", { terminal_id: "term_1" }).target).toBe("term_1");
    expect(toolHeading("terminal_read", { terminal_id: "call-1" }).target).toBe("call-1");
    expect(toolHeading("terminal_stop", { terminal_id: "call-1" })).toEqual({ title: "停止命令", target: "call-1" });
    // 流式参数里命令先于终端 id 到达时不拼出「null · 命令」。
    expect(toolHeading("terminal_write", { command: "npm test" }).target).toBeNull();
    expect(toolHeading("mcp", { action: "call", server_name: "fs", tool_name: "read" })).toEqual({ title: "调用 MCP", target: "fs/read" });
    expect(toolHeading("mcp", { action: "list", server_name: "fs" }).target).toBe("fs");
    expect(toolHeading("skill", { action: "load", skill_name: "pdf", resource_path: "ref.md" }).target).toBe("pdf/ref.md");
    expect(toolHeading("send_message", { task_id: 12 }).target).toBe("#12");
  });

  it("目录里没有的工具退回工具名，没有图标", () => {
    expect(toolHeading("future_tool", { x: 1 })).toEqual({ title: "future_tool", target: null });
    expect(toolVerb("future_tool")).toBe("future_tool");
    expect(toolIconName("future_tool")).toBeUndefined();
  });
});

describe("TOOL_CATALOG", () => {
  // 回归守卫：曾经的 Prepare / 配置口径让人把 agent_team 当成建 Team 的工具，而它只运行已有配置。
  it("agent_team 的动词只说「运行」", () => {
    const verb = toolVerb("agent_team");
    for (const forbidden of ["prepare", "configur", "配置", "准备", "创建"]) {
      expect(verb.toLowerCase()).not.toContain(forbidden);
    }
  });
});
