import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));
// 服务端渲染读 zustand 的初始状态；任务状态推送的镜像换成可控的表。
const taskStatuses = vi.hoisted(() => new Map<number, string | null>());
vi.mock("@/lib/task-status/task-status-store", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/task-status/task-status-store")>(),
  useTaskExecutionStatus: (taskId: number | null | undefined) => (taskId == null ? null : taskStatuses.get(taskId) ?? null),
}));

import { CallRow } from "@/components/assistant-ui/tools/call-row";
import { useDisclosureStore } from "@/lib/assistant/disclosure-store";
import type { ToolArtifact } from "@/lib/assistant/tool-artifact";

function artifact(overrides: Partial<ToolArtifact> = {}): ToolArtifact {
  return {
    backendStatus: "completed",
    awaitingUser: false,
    background: false,
    display_data: null,
    error: null,
    errorCode: null,
    ...overrides,
  };
}

type RenderProps = { interactive?: boolean; taskCancelling?: boolean; toolName?: string; args?: Record<string, unknown> };

function render(overrides: Partial<ToolArtifact> = {}, { toolName = "read_file", args = {}, ...props }: RenderProps = {}): string {
  return renderToStaticMarkup(
    <CallRow taskId={7} toolCallId="call-1" toolName={toolName} args={args} artifact={artifact(overrides)} {...props} />,
  );
}

beforeEach(() => {
  useDisclosureStore.setState({ tasks: {} });
  taskStatuses.clear();
});

describe("CallRow 标题与目标", () => {
  it("标题、目标与图标按工具目录从参数算出", () => {
    const read = render({}, { args: { path: "src/a.ts" } });
    expect(read).toContain("读取文件");
    expect(read).toContain("src/a.ts");
    expect(read).toContain("lucide-eye");
  });

  it("命令的标题是模型写的 description，目标是命令首行", () => {
    const html = render({}, { toolName: "execute_terminal", args: { description: "运行后端单元测试", command: "uv run pytest -q\necho done" } });
    expect(html).toContain("运行后端单元测试");
    expect(html).toContain("uv run pytest -q");
    expect(html).not.toContain("echo done");
    expect(html).toContain("lucide-terminal");
  });

  it("目录里没有的工具退回工具名与扳手图标", () => {
    const html = render({}, { toolName: "future_tool" });
    expect(html).toContain("future_tool");
    expect(html).toContain("lucide-wrench");
  });

  it("排队且还没有参数时标题闪烁，参数到达后不再闪烁", () => {
    expect(render({ backendStatus: "pending" })).toContain("shimmer");
    expect(render({ backendStatus: "pending" }, { args: { path: "a.ts" } })).not.toContain("shimmer");
  });
});

describe("CallRow 状态位", () => {
  it("运行中显示旋转、失败显示告警、取消显示叉；成功不画对勾", () => {
    expect(render({ backendStatus: "running" })).toContain("animate-spin");
    expect(render({ backendStatus: "failed", error: "路径无效" })).toContain("lucide-circle-alert");
    expect(render({ backendStatus: "cancelled", error: "已取消" })).toContain("lucide-circle-x");
    const done = render();
    expect(done).not.toContain("lucide-check");
    expect(done).not.toContain("animate-spin");
  });

  it("失败把后端短提示放在元信息、标题变红且不可展开；取消用删除线", () => {
    const failed = render({ backendStatus: "failed", error: "路径无效", display_data: { kind: "read-file-meta", path: "a.ts" } });
    expect(failed).toContain("路径无效");
    expect(failed).toContain("text-destructive");
    expect(failed).not.toContain("aria-expanded");
    expect(render({ backendStatus: "cancelled", error: "已取消" })).toContain("line-through");
  });

  it("结束后标题转灰，运行中保持前景色", () => {
    expect(render()).toMatch(/data-slot="call-title"[^>]*text-muted-foreground/);
    expect(render({ backendStatus: "running" })).toMatch(/data-slot="call-title"[^>]*text-foreground/);
  });
});

describe("CallRow 附加态", () => {
  it("等待用户在元信息里标「等你操作」", () => {
    expect(render({ backendStatus: "pending", awaitingUser: true })).toContain("等你操作");
  });

  it("后台占位未结束时标「后台运行中」，结束后不再标", () => {
    expect(render({ backendStatus: "pending", background: true })).toContain("后台运行中");
    expect(render({ backendStatus: "completed", background: true })).not.toContain("后台运行中");
  });
});

describe("CallRow kind 视图", () => {
  it("有展开体的 kind 整行是折叠按钮，元信息来自视图", () => {
    const html = render({
      display_data: { kind: "content-search-results", pattern: "tick", match_count: 3, matches: [{ path: "a.ts", line: 1, content: "tick" }] },
    });
    expect(html).toMatch(/<button[^>]*aria-expanded="false"[^>]*data-slot="call-row"/);
    expect(html).toContain("tick · 3 个命中");
  });

  it("未知 kind 不崩溃、没有元信息也不可展开", () => {
    const html = render({ display_data: { kind: "future-kind", payload: "x" } });
    expect(html).toContain("读取文件");
    expect(html).not.toContain("aria-expanded");
    expect(html).not.toContain("payload");
  });

  it("命令退出码非 0 时元信息给 exit N 并默认展开输出", () => {
    const html = render({
      display_data: { kind: "terminal-result", command: "bun run typecheck", output: "error TS2304", exit_code: 2, workdir: "/w" },
    }, { toolName: "execute_terminal", args: { description: "运行类型检查", command: "bun run typecheck" } });
    expect(html).toContain("exit 2");
    expect(html).toContain('data-terminal-viewport="true"');
  });

  it("命令运行中默认展开、结束即收起（没有用户覆盖时）", () => {
    const running = render({ backendStatus: "running", display_data: { kind: "terminal-result", command: "make", output: "building" } });
    expect(running).toContain('data-terminal-viewport="true"');
    const done = render({ display_data: { kind: "terminal-result", command: "make", output: "built", exit_code: 0 } });
    expect(done).not.toContain('data-terminal-viewport="true"');
    expect(done).not.toContain("exit 0");
  });

  it("委派行的状态读任务状态推送，不读展示数据", () => {
    taskStatuses.set(501, "running");
    const html = render({
      childTaskId: 501,
      display_data: { kind: "delegation-result", title: "审查代码", child_task_id: 501, status: "completed" },
    });
    expect(html).toContain("运行中");
    expect(html).not.toContain("已完成");
  });

  it("只读面不渲染停止与转到后台", () => {
    const html = render({ backendStatus: "running", display_data: { kind: "terminal-result", command: "make", output: "" } }, { interactive: false });
    expect(html).not.toContain("停止终端命令");
    expect(html).not.toContain("转到后台");
  });
});
