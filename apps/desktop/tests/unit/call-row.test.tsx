import { QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
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
import { createQueryClient } from "@/lib/query/query-client";
import type { ToolArtifact } from "@/lib/assistant/tool-artifact";

function artifact(overrides: Partial<ToolArtifact> = {}): ToolArtifact {
  return {presentation: "activity",
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
  // 终端命令的展开体经 `useOpenLink` 接上链接打开，需要 Query 上下文（应用里由根组件提供）。
  return renderToStaticMarkup(
    <QueryClientProvider client={createQueryClient()}>
      <CallRow taskId={7} toolCallId="call-1" toolName={toolName} args={args} artifact={artifact(overrides)} {...props} />
    </QueryClientProvider>,
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

  it("命令的标题使用统一 intent，不重复命令", () => {
    const html = render({ intent: "运行后端单元测试" }, { toolName: "execute_terminal", args: { command: "uv run pytest -q\necho done" } });
    expect(html).toContain("运行后端单元测试");
    expect(html).not.toContain("uv run pytest -q");
    expect(html).not.toContain("echo done");
    expect(html).toContain("lucide-terminal");
  });

  it("目录里没有的工具退回工具名与扳手图标", () => {
    const html = render({}, { toolName: "future_tool" });
    expect(html).toContain("future_tool");
    expect(html).toContain("lucide-wrench");
  });

  it("排队状态扫光，参数到达不隐藏状态", () => {
    expect(render({presentation: "activity",  backendStatus: "pending" })).toContain("shimmer");
    expect(render({presentation: "activity",  backendStatus: "pending" }, { args: { path: "a.ts" } })).toContain("shimmer");
  });
});

describe("CallRow 状态位", () => {
  it("运行中状态文字扫光、失败显示告警、取消显示叉；成功不画对勾", () => {
    expect(render({presentation: "activity",  backendStatus: "running" })).toContain("shimmer");
    expect(render({presentation: "activity",  backendStatus: "failed", error: "路径无效" })).toContain("lucide-circle-alert");
    expect(render({presentation: "activity",  backendStatus: "cancelled", error: "已取消" })).toContain("lucide-circle-x");
    const done = render();
    expect(done).not.toContain("lucide-check");
    expect(done).not.toContain("animate-spin");
  });

  it("失败把后端短提示放在元信息、标题变红且不可展开；取消保持中性、不抹去操作记录", () => {
    const failed = render({presentation: "activity",  backendStatus: "failed", error: "路径无效", display_data: { kind: "read-file-meta", path: "a.ts" } });
    expect(failed).toContain("路径无效");
    expect(failed).toContain("text-destructive");
    expect(failed).not.toContain("aria-expanded");
    expect(render({presentation: "activity",  backendStatus: "cancelled", error: "已取消" })).not.toContain("line-through");
  });

  it("结束后标题转灰，运行中保持前景色", () => {
    expect(render()).toMatch(/data-slot="call-title"[^>]*text-muted-foreground/);
    expect(render({presentation: "activity",  backendStatus: "running" })).toMatch(/data-slot="call-title"[^>]*text-foreground/);
  });
});

describe("CallRow 附加态", () => {
  it("等待用户在元信息里标「等你操作」", () => {
    const html = render({presentation: "activity",  backendStatus: "pending", awaitingUser: true });
    expect(html).toContain("等你操作");
    expect(html).not.toContain("animate-spin");
    expect(html).toContain("等待你的操作");
  });

  it("后台占位未结束时标「后台运行中」，结束后不再标", () => {
    expect(render({presentation: "activity",  backendStatus: "pending", background: true })).toContain("后台运行中");
    expect(render({presentation: "activity",  backendStatus: "completed", background: true })).not.toContain("后台运行中");
  });
});

describe("CallRow kind 视图", () => {
  it("有展开体的 kind 整行是折叠按钮，元信息来自视图", () => {
    const html = render({
      display_data: { kind: "content-search-results", pattern: "tick", match_count: 3, matches: [{ path: "a.ts", line: 1, content: "tick" }] },
    });
    expect(html).toMatch(/<button[^>]*aria-expanded="false"[^>]*data-slot="call-row"/);
    expect(html).toContain("3 个命中");
  });

  it.each(["failed", "cancelled"] as const)("%s 保留实际终端输出的展开入口", (backendStatus) => {
    const html = render({presentation: "activity",  backendStatus, error: "已停止", display_data: {
      kind: "terminal-result", command: "make", output: "partial", exit_code: -1,
    } }, { toolName: "execute_terminal" });
    expect(html).not.toContain('data-terminal-viewport="true"');
    expect(html).toContain("已停止");
    expect(html).toContain("exit -1");
  });

  it("未知 kind 不崩溃、没有元信息也不可展开", () => {
    const html = render({ display_data: { kind: "future-kind", payload: "x" } });
    expect(html).toContain("读取文件");
    expect(html).not.toContain("aria-expanded");
    expect(html).not.toContain("payload");
  });

  it("命令退出码非 0 时元信息给 exit N 并保留折叠输出入口", () => {
    const html = render({
      display_data: { kind: "terminal-result", command: "bun run typecheck", output: "error TS2304", exit_code: 2, workdir: "/w" },
    }, { toolName: "execute_terminal", args: { description: "运行类型检查", command: "bun run typecheck" } });
    expect(html).toContain("exit 2");
    expect(html).not.toContain('data-terminal-viewport="true"');
  });

  it("命令运行中与结束后都默认折叠（没有用户覆盖时）", () => {
    const running = render({presentation: "activity",  backendStatus: "running", display_data: { kind: "terminal-result", command: "make", output: "building" } });
    expect(running).not.toContain('data-terminal-viewport="true"');
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

  it("分叉行跳到它的侧栏对话，状态同样读任务状态推送", () => {
    taskStatuses.set(41, "running");
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <CallRow
          taskId={7}
          toolCallId="call-fork"
          toolName="fork_task"
          args={{ title: "前端部分", message: "只做前端" }}
          artifact={artifact({ childTaskId: 41, display_data: { kind: "fork-task-result", title: "前端部分", child_task_id: 41 } })}
        />
      </MemoryRouter>,
    );
    expect(html).toContain("分叉对话");
    expect(html).toContain("前端部分");
    expect(html).toContain("运行中");
    expect(html).toContain("打开分叉对话：前端部分");
    expect(html).toContain("停止分叉对话：前端部分");
  });

  it("消息卡片可以打开收件方，但不授予停止对方的能力", () => {
    taskStatuses.set(41, "running");
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <CallRow taskId={9} toolCallId="call-send" toolName="send_message"
          args={{ task_id: 41, message: "进展" }}
          artifact={artifact({ display_data: {
            kind: "agent-task-result", operation: "send", task_id: 41, agent_name: "前端部分",
          } })}
        />
      </MemoryRouter>,
    );
    expect(html).toContain("打开任务：前端部分");
    expect(html).not.toContain("停止任务");
  });

  it("只读面不渲染停止与转到后台", () => {
    const html = render({presentation: "activity",  backendStatus: "running", display_data: { kind: "terminal-result", command: "make", output: "" } }, { interactive: false });
    expect(html).not.toContain("停止终端命令");
    expect(html).not.toContain("转到后台");
  });
});
