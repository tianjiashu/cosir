import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { resolveCallView } from "@/components/conversation/tools/call-view-registry";
import type { CallViewContext } from "@/components/conversation/tools/call-view";
import { PathText } from "@/components/workspace-references/workspace-path-link";
import { toolResultSignal } from "@/lib/assistant/tools/tool-result-signals";
import type { ToolArtifact } from "@/lib/assistant/tools/tool-artifact";
import { terminalBadge, terminalTitle } from "@/lib/terminal/terminal-sessions";
import type { TerminalSessionEntry } from "@/lib/assistant/contract";

/**
 * 调用行规则：每行只有「标题 · 目标 · 一个结果量 · 一个问题信号」。潜在缺陷：元信息重复目标、
 * 行上出现没用的量、问题说法含糊、路径从尾部截掉文件名、终端只露 id。
 */
const view = (display_data: Record<string, unknown>, args: Record<string, unknown> = {}) => {
  const artifact: ToolArtifact = { presentation: "activity", backendStatus: "completed", awaitingUser: false, background: false, display_data, error: null };
  const context: CallViewContext = { artifact, sessionId: 1, toolCallId: "c", toolName: "probe", args, interactive: true, sessionCancelling: false };
  return resolveCallView(context);
};
const html = (node: unknown) => renderToStaticMarkup(<>{node as React.ReactNode}</>);

describe("meta says only what the target does not", () => {
  it("drops the file size, the repeated Skill name and the repeated command", () => {
    expect(view({ kind: "read-file-meta", path: "a.ts", line_range: { start: 1, end: 40 }, file_size: 12_000 })?.meta).toBe("L1-L40");
    expect(view({ kind: "skill-content", action: "load", skill_name: "pdf", scope: "system" })?.meta).toBeUndefined();
    expect(view({ kind: "skill-content", action: "load", skill_name: "pdf", scope: "workspace" })?.meta).toBe("工作区");
    expect(html(view({ kind: "terminal-session", terminal_id: "t1", input_summary: "npm run dev" }, { command: "npm run dev" })?.meta)).toBe("等你回车");
  });

  // 目标只是文字（行可能整行是按钮），打开文件放在行尾「打开」。
  it("marks a single new file and opens its path from the row actions", () => {
    const presentation = view({ kind: "file-changes", changes: [{ path: "src/router.ts", status: "added", patch: null, insertions: 48, deletions: 0 }] });
    expect(html(presentation?.meta)).toContain("新建");
    expect(html(presentation?.meta)).toContain("+48");
    expect(presentation?.target).toBeUndefined();
    expect((presentation?.actions as React.ReactElement<{ path: string }>).props.path).toBe("src/router.ts");
  });
});

describe("problem signals", () => {
  it("counts failed images and says an uncertain browser action before a failed one", () => {
    expect(toolResultSignal({ kind: "image-read", images: [], failures: [{ path: "a", hint: "x" }] })).toBe("1 张失败");
    const steps = (...statuses: string[]) => ({ kind: "browser-page", steps: statuses.map((status) => ({ action: "click", target: null, status })) });
    expect(toolResultSignal(steps("done", "unknown", "skipped"))).toBe("结果不确定");
    expect(toolResultSignal(steps("done", "failed", "skipped"))).toBe("动作未全部完成");
    expect(toolResultSignal(steps("done", "done"))).toBeNull();
  });
});

describe("names users recognize", () => {
  it("keeps the file name when the directory is cut", () => {
    const markup = renderToStaticMarkup(<PathText path="src/components/conversation/tools/call-row.tsx" suffix=":42" />);
    expect(markup).toContain(">src/components/conversation/tools/</span>");
    // 文件名交给全仓唯一的文件名截断规则（保住扩展名与它前面几个字符），行号不参与截断。
    expect(markup).toContain('data-slot="middle-truncate"');
    expect(markup).toContain(">-row.tsx</span>");
    expect(markup).toContain(">:42</span>");
    // 分段显示对读屏隐藏，无障碍名称是一份完整路径（分段会被读成「src/ re view.py」）。
    expect(markup).toContain('<span class="sr-only">src/components/conversation/tools/call-row.tsx:42</span>');
    expect(markup).toContain('aria-hidden="true"');
    expect(renderToStaticMarkup(<PathText path="README.md" />)).not.toContain("shrink-[1000]");
  });

  it("calls a terminal by the same name the badge and the tab use", () => {
    const entry = (id: string, status: TerminalSessionEntry["status"], exitCode: number | null = null): TerminalSessionEntry => ({
      terminalSessionId: id, shellKind: "zsh", status, endReason: null, exitCode, integration: "ready", commandRunning: false, prefillPending: false, createdAt: "2026-01-01",
    });
    const sessions = [entry("a", "running"), entry("b", "running"), entry("c", "exited", 1)];
    expect([terminalTitle(sessions, "a"), terminalTitle(sessions, "c"), terminalTitle(sessions, "x")]).toEqual(["终端 1", "终端 3", null]);
    expect(terminalTitle([sessions[0]], "a")).toBe("终端");
    expect(terminalBadge(sessions, []).hint).toContain("「终端 3」");
  });
});
