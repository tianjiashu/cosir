// @vitest-environment happy-dom
/**
 * ChangesDrawer 渲染测试。
 *
 * 验证「Changes 从右侧栏上移到对话上方折叠」的核心不变量：
 * 1. 无活跃任务时不挂载（不占用对话上方空间）。
 * 2. 有活跃任务时渲染折叠触发器，且默认收起（不展开文件列表）。
 * 3. 点击触发器展开后，复用 ChangesPanel 内容正确呈现。
 */
import { describe, expect, it, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ChangesDrawer } from "@/components/layout/ChangesDrawer";
import { useTaskStore } from "@/stores/taskStore";
import type { TaskRecord } from "@shared/task";

// mock ChangesPanel，避免真实网络请求，仅验证挂载与展开行为。
vi.mock("@/components/right-panel/ChangesTab", () => ({
  ChangesPanel: () => {
    return <div data-testid="changes-panel-mock">mock changes panel</div>;
  },
}));

function setActiveTask(taskId: string | null) {
  const task = taskId
    ? ({
        task_id: taskId,
        workspace_id: "ws-1",
        agent_id: "agent-1",
        input_text: "t",
        title: "t",
        last_message_preview: "",
        latest_turn_id: null,
        status: "running",
        execution_status: "running",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      } satisfies TaskRecord)
    : null;
  act(() => {
    useTaskStore.setState({
      activeTaskId: taskId,
      activeTurnId: null,
      selectedAgentId: "developer",
      tasksById: task ? { [task.task_id]: task } : {},
      tasksByWorkspaceId: taskId
        ? {
            "ws-1": [task as TaskRecord],
          }
        : {},
    });
  });
}

describe("ChangesDrawer 折叠行为", () => {
  beforeEach(() => {
    setActiveTask(null);
  });

  it("无活跃任务时不挂载（无 Changes 触发器）", () => {
    const { container } = render(<ChangesDrawer />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("Changes")).toBeNull();
  });

  it("有活跃任务时渲染触发器且默认折叠", () => {
    setActiveTask("task-1");
    render(<ChangesDrawer />);

    // 触发器存在
    expect(screen.getByText("Changes")).toBeTruthy();
    // 默认折叠：ChangesPanel 内容不应出现
    expect(screen.queryByTestId("changes-panel-mock")).toBeNull();
  });

  it("点击展开后出现两个子区，文件列表默认展开、任务列表占位需展开子折叠", () => {
    setActiveTask("task-1");
    render(<ChangesDrawer />);

    const trigger = screen.getByText("Changes");
    act(() => {
      fireEvent.click(trigger);
    });
    // 展开后出现两个并列子区触发器
    expect(screen.getByText("任务列表")).toBeTruthy();
    expect(screen.getByText("文件列表")).toBeTruthy();
    // 文件列表子区默认展开：ChangesPanel 内容挂载
    expect(screen.getByTestId("changes-panel-mock")).toBeTruthy();
    // 任务列表子区默认收起：占位不可见
    expect(screen.queryByText("任务列表即将上线")).toBeNull();

    // 展开任务列表子折叠：占位出现（预留扩展位）
    act(() => {
      fireEvent.click(screen.getByText("任务列表"));
    });
    expect(screen.getByText("任务列表即将上线")).toBeTruthy();

    act(() => {
      fireEvent.click(trigger);
    });
    // 收起后内容消失
    expect(screen.queryByTestId("changes-panel-mock")).toBeNull();
    expect(screen.queryByText("任务列表即将上线")).toBeNull();
  });
});
