/**
 * string↔number 类型统一修复的核心不变量验证。
 *
 * 背景：乐观更新期前端用「负 number 占位」（-1 - seq）作为临时 task/turn 的
 * TurnRecord.task_id / TurnRecord.turn_id，store 全用 number 主通道；真实返回后
 * 经 replaceTask / replaceTurnId 以 number 占位定位替换。此测试验证替换逻辑能
 * 正确定位并移除临时记录（修复前 oldTurnId 为 string、item.turn_id 为 number，
 * 比对 `!==` 永远为 true 导致临时记录不被移除的 bug）。
 */

import { useTurnStore } from "@/stores/turnStore";
import { useTaskStore } from "@/stores/taskStore";
import type { TurnRecord } from "@shared/turn";
import type { TaskRecord } from "@shared/task";

function makeTurn(taskId: number, turnId: number): TurnRecord {
  const now = new Date().toISOString();
  return {
    turn_id: turnId,
    task_id: taskId,
    input_text: "x",
    status: "pending",
    end_reason: null,
    response_text: null,
    created_at: now,
    updated_at: now,
  };
}

function makeTask(taskId: number, workspaceId: number): TaskRecord {
  const now = new Date().toISOString();
  return {
    task_id: taskId,
    workspace_id: workspaceId,
    title: "t",
    status: "pending",
    execution_status: "pending",
    created_at: now,
    updated_at: now,
  };
}

describe("replaceTurnId 负 number 占位替换", () => {
  it("用负 number 占位能正确移除临时 turn 并写入真实 turn", () => {
    const taskId = 100;
    const tempTurnId = -1; // 负 number 占位
    useTurnStore.getState().setTurnsForTask(taskId, [makeTurn(taskId, tempTurnId)]);

    const realTurn = makeTurn(taskId, 555);
    useTurnStore.getState().replaceTurnId(taskId, tempTurnId, realTurn);

    const turns = useTurnStore.getState().turnsByTaskId[taskId];
    expect(turns).toHaveLength(1);
    expect(turns[0].turn_id).toBe(555);
    expect(turns[0].turn_id).not.toBe(tempTurnId);
  });

  it("真实返回后仍保留其它真实 turn 不被误删", () => {
    const taskId = 101;
    useTurnStore.getState().setTurnsForTask(taskId, [
      makeTurn(taskId, 10),
      makeTurn(taskId, -2), // 临时占位
      makeTurn(taskId, 11),
    ]);
    useTurnStore.getState().replaceTurnId(taskId, -2, makeTurn(taskId, 777));
    const ids = useTurnStore.getState().turnsByTaskId[taskId].map((t) => t.turn_id);
    // replaceTurnId 将真实 turn 追加到列表末尾（与 upsertTurn 的同末尾追加语义一致）；
    // 关键不变量：临时占位 -2 已被正确移除，其它真实 turn (10,11) 保留。
    expect(ids).toEqual([10, 11, 777]);
    expect(ids).not.toContain(-2);
  });
});

describe("replaceTask 负 number 占位替换与草稿迁移", () => {
  it("用负 number 占位能正确替换临时 task 并迁移草稿", () => {
    const workspaceId = 5;
    const tempTaskId = -3;
    useTaskStore.getState().addTask(makeTask(tempTaskId, workspaceId));
    useTaskStore.getState().setInputDraft(tempTaskId, "草稿内容");

    const realTask = makeTask(888, workspaceId);
    useTaskStore.getState().replaceTask(tempTaskId, realTask);

    const byId = useTaskStore.getState().tasksById;
    expect(byId[tempTaskId]).toBeUndefined();
    expect(byId[888]?.task_id).toBe(888);
    // 草稿应从临时占位迁到真实 id
    expect(useTaskStore.getState().drafts[888]).toBe("草稿内容");
    expect(useTaskStore.getState().drafts[tempTaskId]).toBeUndefined();
  });
});
