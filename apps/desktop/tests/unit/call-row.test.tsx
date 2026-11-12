import { QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn().mockResolvedValue(undefined) }));
// 服务端渲染读 zustand 的初始状态；任务状态推送的镜像换成可控的表。
const taskStatuses = vi.hoisted(() => new Map<number, string | null>());
const awaitingCalls = vi.hoisted(() => new Map<number, number>());
vi.mock("@/lib/task-status/task-status-store", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/task-status/task-status-store")>(),
  useTaskExecutionStatus: (taskId: number | null | undefined) => (taskId == null ? null : taskStatuses.get(taskId) ?? null),
  useTaskAwaitingUserCalls: (taskId: number | null | undefined) => (taskId == null ? 0 : awaitingCalls.get(taskId) ?? 0),
}));

// 开合与前台等待同样读 store：换成可控的表（终端卡片在展开体里，去留操作看前台等待）。
const surface = vi.hoisted(() => ({ open: new Set<string>(), waits: new Map<number, string[]>() }));
vi.mock("@/lib/assistant/turn/disclosure-store", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/assistant/turn/disclosure-store")>(),
  useDisclosure: (_taskId: number, key: string, defaultOpen: boolean) => [surface.open.has(key) || defaultOpen, () => undefined],
}));
vi.mock("@/lib/background-tasks/background-tasks-store", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/background-tasks/background-tasks-store")>(),
  useForegroundWaits: (taskId: number | null) => (taskId === null ? [] : surface.waits.get(taskId) ?? []),
}));

import { CallRow } from "@/components/conversation/tools/call-row";
import { callKey, useDisclosureStore } from "@/lib/assistant/turn/disclosure-store";
import { createQueryClient } from "@/lib/query/query-client";
import type { ToolArtifact } from "@/lib/assistant/tools/tool-artifact";

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

type RenderProps = { interactive?: boolean; inherited?: boolean; taskCancelling?: boolean; toolName?: string; args?: Record<string, unknown> };

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
  surface.open.clear();
  surface.waits.clear();
  taskStatuses.clear();
  awaitingCalls.clear();
});

/** 展开 `call-1` 的详情（终端卡片在展开体里）。 */
function openCall(): void {
  surface.open.add(callKey("call-1"));
}

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

  it("排队时标题扫光，参数到达前后形态不变", () => {
    expect(render({presentation: "activity",  backendStatus: "pending" })).toMatch(/data-slot="call-title"[^>]*shimmer/);
    expect(render({presentation: "activity",  backendStatus: "pending" }, { args: { path: "a.ts" } })).toMatch(/data-slot="call-title"[^>]*shimmer/);
  });
});

describe("CallRow 状态位", () => {
  it("运行中标题扫光、状态文字只留给读屏，失败显示告警、取消显示叉；成功不画对勾", () => {
    const running = render({presentation: "activity",  backendStatus: "running" });
    expect(running).toMatch(/data-slot="call-title"[^>]*shimmer/);
    expect(running).toContain('<span class="sr-only">执行中</span>');
    expect(render({presentation: "activity",  backendStatus: "failed", error: "路径无效" })).toContain("lucide-circle-alert");
    expect(render({presentation: "activity",  backendStatus: "cancelled", error: "已取消" })).toContain("lucide-circle-x");
    const done = render();
    expect(done).not.toContain("lucide-check");
    expect(done).not.toContain("animate-spin");
    expect(done).not.toContain("shimmer");
    expect(done).not.toContain("已完成");
  });

  it("失败把后端短提示放在元信息、标题变红且不可展开；取消保持中性、不抹去操作记录", () => {
    const failed = render({presentation: "activity",  backendStatus: "failed", error: "路径无效", display_data: { kind: "read-file-meta", path: "a.ts" } });
    expect(failed).toContain("路径无效");
    expect(failed).toContain("text-destructive");
    expect(failed).not.toContain("aria-expanded");
    expect(render({presentation: "activity",  backendStatus: "cancelled", error: "已取消" })).not.toContain("line-through");
  });

  it("继承时未结束的调用不扫光：它已经没有执行者", () => {
    const html = render({presentation: "activity",  backendStatus: "running" }, { inherited: true });
    expect(html).not.toContain("shimmer");
    expect(html).toContain("继承时未结束");
  });
});

describe("CallRow 附加态", () => {
  it("等待用户在元信息里标「等你操作」", () => {
    const html = render({presentation: "activity",  backendStatus: "pending", awaitingUser: true });
    expect(html).toContain("等你操作");
    expect(html).not.toContain("animate-spin");
    expect(html).toContain("等待你的操作");
  });

  it("后台占位未结束时标题扫光，结束后停止", () => {
    expect(render({presentation: "activity",  backendStatus: "pending", background: true })).toMatch(/data-slot="call-title"[^>]*shimmer/);
    const done = render({presentation: "activity",  backendStatus: "completed", background: true });
    expect(done).not.toContain("shimmer");
    expect(done).not.toContain("后台运行中");
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
      kind: "terminal-result", command: "make", output: "partial", end: backendStatus === "failed" ? "timed_out" : "cancelled",
    } }, { toolName: "execute_terminal" });
    expect(html).not.toContain('data-terminal-viewport="true"');
    expect(html).toContain("已停止");
    // 被停下的命令没有退出码：不再显示旧的 -1 占位。
    expect(html).not.toContain("exit -1");
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

  it("被信号结束的命令显示信号而不是退出码", () => {
    const html = render({ display_data: { kind: "terminal-result", command: "./server", output: "", end: "signaled", signal: 9 } }, { toolName: "execute_terminal" });
    expect(html).toContain("signal 9");
    expect(html).not.toContain("exit ");
  });

  it("命令运行中与结束后都默认折叠（没有用户覆盖时）", () => {
    const running = render({presentation: "activity",  backendStatus: "running", display_data: { kind: "terminal-result", command: "make", output: "building" } });
    expect(running).not.toContain('data-terminal-viewport="true"');
    const done = render({ display_data: { kind: "terminal-result", command: "make", output: "built", end: "exited", exit_code: 0 } });
    expect(done).not.toContain('data-terminal-viewport="true"');
    expect(done).not.toContain("exit 0");
  });

  const fork = (overrides: Partial<ToolArtifact> = {}, props: { interactive?: boolean } = {}) => renderToStaticMarkup(
    <MemoryRouter>
      <CallRow
        taskId={7}
        toolCallId="call-fork"
        toolName="fork_task"
        args={{ title: "前端部分", message: "只做前端" }}
        artifact={artifact({ childTaskId: 41, display_data: { kind: "fork-task-result", title: "前端部分", child_task_id: 41 }, ...overrides })}
        {...props}
      />
    </MemoryRouter>,
  );

  it.each(["running", "pending", "completed", null])("子任务 %s 时委派行不说状态，且不读展示数据", (status) => {
    taskStatuses.set(501, status);
    const html = render({
      childTaskId: 501,
      display_data: { kind: "delegation-result", title: "审查代码", child_task_id: 501, status: "completed" },
    });
    expect(html).not.toContain("运行中");
    expect(html).not.toContain("已完成");
  });

  it.each([["failed", "运行失败"], ["cancelled", "已停止"], ["suspended", "等待结果"]])("子任务 %s 时委派行报异常", (status, label) => {
    taskStatuses.set(501, status);
    const html = render({ childTaskId: 501, display_data: { kind: "delegation-result", title: "审查代码", child_task_id: 501 } });
    expect(html).toContain(label);
  });

  it("子任务有事等你操作时，不论它的 Run 怎样，调用行都写明等你操作", () => {
    taskStatuses.set(501, "completed");
    awaitingCalls.set(501, 1);
    const html = render({ childTaskId: 501, display_data: { kind: "delegation-result", title: "审查代码", child_task_id: 501 } });
    expect(html).toContain("等你操作");
    expect(html).not.toContain("已完成");
  });

  it("工具自身取消时不再叠子任务状态", () => {
    taskStatuses.set(41, "cancelled");
    const html = fork({ backendStatus: "cancelled", error: "已取消" });
    expect(html).toContain("已取消");
    expect(html).not.toContain("已停止");
  });

  it("后台分叉在时间线只留一行：点行定位看板，没有打开、停止与展开", () => {
    taskStatuses.set(41, "running");
    const html = fork({ backendStatus: "pending", background: true });
    expect(html).toContain("在后台任务中查看：");
    expect(html).toMatch(/<button[^>]*data-slot="call-row"/);
    expect(html).not.toContain("aria-expanded");
    expect(html).not.toContain("打开分叉对话");
    expect(html).not.toContain("停止分叉对话");
  });

  it("只读阅读器里当前 Task 没有看板：分叉仍可打开与展开，打开悬停才显示", () => {
    const html = fork({ background: true }, { interactive: false });
    expect(html).toContain("打开分叉对话：前端部分");
    expect(html.match(/<button[^>]*打开分叉对话[^>]*>/)?.[0]).toContain("opacity-0");
    expect(html).toContain("aria-expanded");
    expect(html).not.toContain("在后台任务中查看");
  });

  it("后台命令同样只留一行，输出与停止都在看板", () => {
    const html = render({ backendStatus: "pending", background: true, display_data: { kind: "terminal-result", command: "make", output: "x" } }, { toolName: "execute_terminal" });
    expect(html).toContain("在后台任务中查看：");
    expect(html).not.toContain("停止终端命令");
    expect(html).not.toContain("aria-expanded");
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

  it("状态查询行保留停止：子任务被消息唤醒后这里是单独停止它的入口", () => {
    taskStatuses.set(41, "running");
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <CallRow taskId={9} toolCallId="call-status" toolName="task_status"
          args={{ task_id: 41 }}
          artifact={artifact({ display_data: { kind: "agent-task-result", operation: "status", task_id: 41, agent_name: "前端部分" } })}
        />
      </MemoryRouter>,
    );
    expect(html).toContain("停止任务：前端部分");
  });

  it("只读面不渲染停止与转到后台", () => {
    const html = render({presentation: "activity",  backendStatus: "running", display_data: { kind: "terminal-result", command: "make", output: "" } }, { interactive: false });
    expect(html).not.toContain("停止终端命令");
    expect(html).not.toContain("转到后台");
  });
});

describe("CallRow 运行时长", () => {
  const startedSecondsAgo = (seconds: number) => new Date(Date.now() - seconds * 1000).toISOString();

  it("运行超过 3 秒才在标题后显示时长，所有工具一样", () => {
    expect(render({ backendStatus: "running", startedAt: startedSecondsAgo(1) })).not.toContain('data-slot="call-elapsed"');
    const html = render({ backendStatus: "running", startedAt: startedSecondsAgo(75) }, { toolName: "mcp", args: { action: "call", server_name: "jira", tool_name: "search" } });
    expect(html).toMatch(/data-slot="call-elapsed"[^>]*>1分钟1[45]秒</);
    expect(html.indexOf('data-slot="call-title"')).toBeLessThan(html.indexOf('data-slot="call-elapsed"'));
  });

  it("没有运行起点（结束、后台、冷重建）或继承记录不显示时长", () => {
    expect(render({ backendStatus: "completed" })).not.toContain('data-slot="call-elapsed"');
    expect(render({ backendStatus: "running", startedAt: startedSecondsAgo(30) }, { inherited: true })).not.toContain('data-slot="call-elapsed"');
  });
});

describe("终端卡片", () => {
  const running = { backendStatus: "running" as const, display_data: { kind: "terminal-result", command: "git pull origin main", workdir: "/w" } };

  it("行上没有停止与转到后台：去留操作在卡片头部", () => {
    surface.waits.set(7, ["call-1"]);
    const collapsed = render(running, { toolName: "execute_terminal" });
    expect(collapsed).not.toContain("停止终端命令");
    expect(collapsed).not.toContain("转到后台");

    openCall();
    const html = render(running, { toolName: "execute_terminal" });
    const header = html.slice(html.indexOf('data-slot="terminal-card-header"'), html.indexOf('data-terminal-command'));
    expect(header).toContain("转到后台");
    expect(header).toContain("停止终端命令");
  });

  it("还没有输出时命令已可见，输出区是占位", () => {
    openCall();
    const html = render(running, { toolName: "execute_terminal" });
    expect(html).toMatch(/data-terminal-command="true"[^>]*>git pull origin main</);
    expect(html).toContain("等待输出…");
  });

  it("不在前台等待表里时只有停止；结束后头部换成状态、操作消失", () => {
    openCall();
    const live = render(running, { toolName: "execute_terminal" });
    expect(live).not.toContain("转到后台");
    expect(live).toContain("停止终端命令");
    const done = render({ display_data: { ...running.display_data, output: "Already up to date.\n", end: "exited", exit_code: 0 } }, { toolName: "execute_terminal" });
    expect(done).not.toContain("停止终端命令");
    expect(done).toContain("已结束");
  });

  it("Task 停止中收起操作，头部说正在停止", () => {
    openCall();
    const html = render(running, { toolName: "execute_terminal", taskCancelling: true });
    expect(html).not.toContain("停止终端命令");
    expect(html).toContain("正在停止…");
  });
});
