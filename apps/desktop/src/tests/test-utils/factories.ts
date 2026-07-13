import type { RuntimeEvent } from "@shared/events";

/** 构造一条测试用 RuntimeEvent。 */
export function makeEvent(
  event_id: string,
  overrides: Partial<RuntimeEvent> = {},
): RuntimeEvent {
  return {
    event_id,
    event_type: "step_started",
    task_id: "task-1",
    created_at: new Date().toISOString(),
    payload: { step_type: "test", step_index: 0 },
    ...overrides,
  };
}

/** 构造一条测试用 TaskRecord。 */
export function makeTask(
  task_id: string,
  overrides: Partial<import("@shared/task").TaskRecord> = {},
): import("@shared/task").TaskRecord {
  return {
    task_id,
    session_id: "sess-1",
    agent_id: "agent-1",
    input_text: "do something",
    status: "running",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}
