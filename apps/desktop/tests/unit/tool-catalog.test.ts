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

  // 目的：目标写用户认得的实体；潜在缺陷：没写意图时一串行全叫「执行命令」、子 Agent 只露配置 id。
  it("命令与页面脚本的标题是它们的 intent 参数（没有时用动词），其余工具只用动词；目标是命令首行与子 Agent 名", () => {
    expect(toolHeading("execute_terminal", { intent: "Run backend unit tests", command: "pytest -q\necho" }))
      .toMatchObject({ title: "Run backend unit tests", target: null });
    expect(toolHeading("browser_evaluate", { intent: "读页面标题", expression: "document.title" }))
      .toMatchObject({ title: "读页面标题", target: null });
    // 不声明意图的工具：参数里即使出现 intent 也不当标题（外部工具的 intent 是业务参数）。
    expect(toolHeading("read_file", { intent: "x", path: "a.py" }).title).toBe("读取文件");
    expect(toolHeading("execute_terminal", { command: "pytest -q\necho" })).toMatchObject({ title: "执行命令", target: "pytest -q" });
    expect(toolHeading("execute_terminal", { command: `npm run ${"x".repeat(80)}` }).target).toHaveLength(61);
    expect(toolHeading("delegate_task", { agent_name: "安全审查员", child_agent_id: "reviewer" }))
      .toMatchObject({ title: "委派任务", target: "安全审查员" });
    expect(toolHeading("fork_session", { title: "换个思路" })).toMatchObject({ title: "分叉会话", target: "换个思路" });
    expect(toolHeading("read_file", { path: "app/main.py" })).toEqual({ title: "读取文件", target: "app/main.py", targetIsPath: true });
    expect(toolHeading("execute_terminal", { command: "ls" }).targetIsPath).toBe(false);
  });

  it("多目标只写首个加 +N，组合目标各段齐全才拼", () => {
    expect(toolHeading("search_content", { pattern: "ToolHeading", path: "app" }).target).toBe('"ToolHeading" in app');
    expect(toolHeading("search_content", { pattern: "x" }).target).toBeNull();
    expect(toolHeading("read_image", { paths: ["a.png", "b.png"] }).target).toBe("a.png +1");
    expect(toolHeading("web_fetch", { urls: ["https://example.com/a", "https://example.com/b"] }).target).toBe("https://example.com/a +1");
    expect(toolHeading("apply_patch", { patch: "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\ndiff --git a/x.py b/x.py\n" }).target).toBe("x.py");
    expect(toolHeading("browser_act", { actions: [{ type: "click", ref: "e1" }, { type: "scroll" }] }).target).toBe("点击 +1");
    expect(toolHeading("browser_logs", { kind: "network", failed_only: true }).target).toBe("network");
    expect(toolHeading("browser_evaluate", { expression: `document.title\n// ${"x".repeat(80)}` }).target).toBe("document.title");
    expect(toolHeading("browser_evaluate", { expression: "a".repeat(70) }).target).toBe(`${"a".repeat(60)}…`);
  });

  it("填入终端只写命令（终端可能由后端挑或新开），其余终端工具按 terminal_id；MCP 与 Skill 按动作拼目标", () => {
    expect(toolHeading("terminal_write", { terminal_id: "term_1", command: "npm test" }).target).toBe("npm test");
    expect(toolHeading("terminal_write", { terminal_id: null, command: "npm login" }).target).toBe("npm login");
    expect(toolHeading("terminal_write", { terminal_id: "term_1" }).target).toBeNull();
    expect(toolHeading("terminal_read", { terminal_id: "call-1" }).target).toBe("call-1");
    expect(toolHeading("terminal_stop", { terminal_id: "call-1" })).toMatchObject({ title: "停止命令", target: "call-1" });
    // 不指定终端时只有命令，同样显示命令。
    expect(toolHeading("terminal_write", { command: "npm test" }).target).toBe("npm test");
    // MCP 工具按名字前缀认成一族，目标取名字里的「服务/工具」。
    expect(toolHeading("mcp__fs__read_file", { path: "a" })).toMatchObject({ title: "调用 MCP", target: "fs/read_file" });
    expect(toolHeading("mcp__classify__run", { intent: "refund" }).title).toBe("调用 MCP");
    expect(toolHeading("skill", { action: "load", skill_name: "pdf", resource_path: "ref.md" }).target).toBe("pdf/ref.md");
    expect(toolHeading("session_send", { session_id: 12 }).target).toBe("#12");
  });

  it("目录里没有的工具退回工具名，没有图标", () => {
    expect(toolHeading("future_tool", { x: 1 })).toMatchObject({ title: "future_tool", target: null });
    expect(toolVerb("future_tool")).toBe("future_tool");
    expect(toolIconName("future_tool")).toBeUndefined();
  });
});

