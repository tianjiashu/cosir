
import { createServer } from "node:http";
import { WebSocketServer } from "ws";

const HOST = "127.0.0.1";
// 端口由 playwright.config.ts 经环境变量传入（见 e2e-env.ts）；手动直跑时回落到 slot 0 的 8000。
const PORT = Number(process.env.COSIR_E2E_API_PORT ?? 8000);
const TASK_ID = 42;
const WORKSPACE_ID = 7;

const workspaces = [
  {
    workspace_id: WORKSPACE_ID,
    name: "demo",
    root_path: "C:/demo",
    created_at: "2026-01-01T00:00:00.000Z",
    updated_at: "2026-01-01T00:00:00.000Z",
  },
];

const tasks = new Map();
const states = new Map();
// taskId -> 打开着的 Task 长订阅；与生产一致：订阅跟随 Task，Run 结束不断开。
const subscribers = new Map();
// taskId -> 正在执行的模拟 Run（同一 Task 至多一个执行者）。
const executors = new Map();
let lastStreamBody = "";
let nextTurn = 1;
let testGeneration = 0;
let dropNextCancelledStream = false;
let failNextAttach = false;
/**
 * 交互终端模拟：taskId -> 会话数组。每个会话持有 snapshot 条目、输出缓冲（seq 递增）与已 attach 的 WS。
 * 只模拟 terminal-v2 的最小闭环（attach 续传、input 回显与短租约、resize、exit），不模拟交接；
 * Agent 持有输入与 shell 退出经 `__test__/terminal/*` 直接改 snapshot 条目。
 */
const terminalSessions = new Map();
/** taskId → 后台工作看板条目（snapshot `background_tasks`），由 e2e 经 `__test__/seed-background-tasks` 种入。 */
const backgroundTasks = new Map();
/**
 * taskId → 前台等待（登记表里的前台条目，wire 上只投影 id 到 `foreground_waits`）。条目带看板字段，
 * 转到后台时原样移入 `backgroundTasks`，与后端「转后台后才算后台工作」一致。
 */
const foregroundWaits = new Map();
let nextTerminalId = 1;
const terminalShells = [
  { kind: "zsh", label: "zsh", isDefault: true, integrationSupported: true },
  { kind: "bash", label: "bash", isDefault: false, integrationSupported: true },
  { kind: "sh", label: "sh", isDefault: false, integrationSupported: false },
];
const telemetry = {
  completedStreamCount: 0,
  requestBodies: [],
};

function resetTestState() {
  testGeneration += 1;
  lastPublishedStatus.clear();
  for (const res of statusSubscribers) writeStatusFrame(res, { type: "full", statuses: {} });
  tasks.clear();
  states.clear();
  for (const set of subscribers.values()) for (const res of set) res.end();
  subscribers.clear();
  executors.clear();
  for (const sessions of terminalSessions.values()) {
    for (const session of sessions) {
      for (const socket of session.sockets) socket.terminate();
    }
  }
  terminalSessions.clear();
  backgroundTasks.clear();
  foregroundWaits.clear();
  nextTerminalId = 1;
  dropNextCancelledStream = false;
  failNextAttach = false;
  lastStreamBody = "";
  nextTurn = 1;
  telemetry.completedStreamCount = 0;
  telemetry.requestBodies = [];
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const isActive = (status) => status === "running" || status === "suspended";
// 与 /model-configs 唯一的模型一致：运行期配置锁定下，运行中发送沿用这份配置。
const DEFAULT_LAUNCH = { modelConfigId: 2, reasoningEffort: null, disabledToolGroups: [] };

const emptyState = () => ({ title: "新对话", messages: [], activity: null });

const activityOf = (turn, status, overrides = {}) => ({modelActivity: null,
  turn: turn,
  status: status,
  resumable: status === "cancelled" || status === "failed",
  endReason: status === "completed" ? "stop" : status === "cancelled" ? "user_cancelled" : null,
  error: null,
  usage: null,
  contextWindowTotal: null,
  launch: DEFAULT_LAUNCH,
  startedAt: "2026-10-27T00:00:00.000Z",
  ...overrides,
});

const outcomeOf = (activity) => ({
  status: activity.status,
  endReason: activity.endReason,
  error: activity.error,
  usage: activity.usage,
  contextWindowTotal: activity.contextWindowTotal,
  durationMs: 0,
});

const textMessage = (id, role, text, status, turn) => ({
  id,
  role,
  parts: role === "assistant"
    ? [
        { type: "text", text, status: status === "completed" ? "completed" : "running" },
      ]
    : [{ type: "text", text, status: "completed" }],
  turn,
  inherited: false,
  outcome: null,
  source: null,
});

/** 一个回合：用户消息 id 为 `user-{turn}`，回复 id 按契约为 `assistant-turn-{turn}`。测试里每个 Run 只有一个回合。 */
function stateWithExchange(previous, text, turn, assistantText, status) {
  const next = structuredClone(previous);
  next.messages.push(
    textMessage(`user-${turn}`, "user", text, "completed", turn),
    textMessage(`assistant-turn-${turn}`, "assistant", assistantText, status, turn),
  );
  next.activity = activityOf(turn, TERMINAL.has(status) ? status : "running", {
    endReason: status === "completed" ? "stop" : status === "failed" ? "max_steps_reached" : status === "cancelled" ? "user_cancelled" : null,
  });
  if (TERMINAL.has(status)) next.messages.at(-1).outcome = outcomeOf(next.activity);
  return next;
}

/** 把当前活动收敛到终态：结局写到 `activity.turn` 那一回合的回复上。 */
function settle(state, status, overrides = {}) {
  const next = structuredClone(state);
  next.activity = { ...next.activity, ...activityOf(next.activity.turn, status), usage: next.activity.usage, contextWindowTotal: next.activity.contextWindowTotal, ...overrides };
  const last = [...next.messages].reverse().find((message) => message.role === "assistant" && message.turn === next.activity.turn);
  if (last) {
    last.outcome = outcomeOf(next.activity);
    for (const part of last.parts) if (part.status === "running") part.status = "completed";
  }
  return next;
}

function toolLifecycleParts(turn, status, toolStatus) {
  return [
    {presentation: "activity",
      type: "tool-call", awaitingUser: false,
      toolCallId: `tool-lifecycle-${turn}`,
      toolName: "search_content",
      args: { pattern: "lifecycle" },
      status: toolStatus,
      error: null,
      display_data: toolStatus === "completed"
        ? {
            kind: "content-search-results",
            pattern: "lifecycle",
            path: ".",
            match_count: 1,
            matches: [{ path: "lifecycle.test.ts", line: 1, content: "lifecycle", is_match: true }],
          }
        : null,
      isError: false,
    },
    { type: "text", text: status === "completed" ? "工具完成" : "", status: status === "running" ? "running" : "completed" },
  ];
}

function usageFixture(secondRun) {
  const input = secondRun ? 2_000 : 1_000;
  const output = 500;
  return {
    input_tokens: input,
    output_tokens: output,
    total_tokens: input + output,
    cache_hit_tokens: secondRun ? 200 : 100,
    cache_miss_tokens: secondRun ? 50 : null,
    reasoning_tokens: secondRun ? 40 : 20,
  };
}

function completedFixture(turn, userMessage, assistantParts) {
  const activity = activityOf(turn, "completed", { launch: { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] } });
  return {
    title: "测试对话",
    messages: [
      { ...userMessage, turn, inherited: false, outcome: null, source: null },
      { id: `assistant-turn-${turn}`, role: "assistant", parts: assistantParts, turn, inherited: false, outcome: outcomeOf(activity), source: null },
    ],
    activity,
  };
}

function toolTraceState() {
  return completedFixture(77, {
    id: "user-tool-trace",
    role: "user",
    parts: [{ type: "text", text: "检查项目", status: "completed" }],
  }, [
    {presentation: "activity",
      type: "tool-call", awaitingUser: false,
      toolCallId: "trace-read-file",
      toolName: "read_file",
      args: { path: "README.md" },
      status: "completed",
      error: null,
      display_data: null,
      isError: false,
    },
    {presentation: "activity",
      type: "tool-call", awaitingUser: false,
      toolCallId: "trace-search-files",
      toolName: "search_content",
      args: { pattern: "assistant-ui" },
      status: "completed",
      error: null,
      display_data: null,
      isError: false,
    },
    { type: "text", text: "检查完成", status: "completed", finalReply: true },
  ]);
}

/**
 * 交替序列（设计文档 §5 场景 A–D 的合成）：思考 → 叙述 → 搜索 → 叙述 → 失败的命令 → 改动 → 交接（等待用户）→ 结论。
 * `phase` 为 running 时结论还没出、最后一步是正在运行的命令；settled 时回合已结束，带耗时。
 */
function alternatingSequenceState(phase) {
  const turn = 79;
  // 图标、动词与标题由前端工具目录按工具名 + 参数算出，夹具只给真实形状的参数。
  const call = (toolCallId, toolName, args, status, extra = {}) => ({presentation: ["agent_team", "delegate_task", "fork_task"].includes(toolName) ? "standalone" : toolName === "wait_messages" ? "quiet" : "activity",
    type: "tool-call", awaitingUser: false, toolCallId, toolName, args, status, error: null,
    display_data: null, isError: false, ...extra,
  });
  const parts = [
    { type: "text", text: "先找调度循环在哪里。", status: "completed" },
    call("alt-search", "search_content", { pattern: "setInterval|tick", path: "src/" }, "completed", {
      display_data: {
        kind: "content-search-results", pattern: "tick", path: "src", match_count: 2,
        matches: [{ path: "src/task.ts", line: 22, content: "const TICK_MS = 250", is_match: true }, { path: "src/config.ts", line: 31, content: "tick", is_match: true }],
      },
    }),
    { type: "text", text: "tick 是写死的常量，配置模块已有校验工具，可以直接加字段。", status: "completed" },
    call("alt-typecheck", "execute_terminal", { command: "bun run typecheck" }, "failed", {
      intent: "运行类型检查", error: "命令执行失败", isError: true, display_data: { status_hint: "命令执行失败" },
    }),
    call("alt-patch", "apply_patch", { patch: "diff --git a/src/config.ts b/src/config.ts\n" }, "completed", {
      display_data: {
        kind: "file-changes",
        diff_stats: { total_files: 2, total_insertions: 30, total_deletions: 6 },
        changes: [
          { path: "src/config.ts", status: "modified", insertions: 9, deletions: 2, patch: "diff --git a/src/config.ts b/src/config.ts\n--- a/src/config.ts\n+++ b/src/config.ts\n@@ -31 +31,2 @@\n-export const tick = 250\n+export const tickMs = read('scheduler.tickMs', 250)\n+export const tickRange = [50, 5000]\n" },
          { path: "src/task.ts", status: "modified", insertions: 21, deletions: 4, patch: null },
        ],
      },
    }),
  ];
  if (phase === "running") {
    parts.push(call("alt-test", "execute_terminal", { command: "bun test tests/task.test.ts" }, "running", {
      intent: "运行单元测试", display_data: { kind: "terminal-result", command: "bun test tests/task.test.ts", workdir: "/work", output: "bun test v1.1\n\ntests/task.test.ts:\n✓ tick reads config\n" },
    }));
  } else {
    parts.push(
      call("alt-handoff", "terminal_handoff", { session_id: "term-alt", reason: "enter the sudo password" }, "pending", {
        awaitingUser: true,
        display_data: { kind: "terminal-session", session_id: "term-alt", status: "running", input_summary: "输入完成后点「交还」，Agent 会继续安装" },
      }),
      { type: "text", text: "已把调度间隔改为读配置项 `scheduler.tickMs`，默认值仍是 250。\n\n新增 3 个单测，`bun test` 全部通过。", status: "completed", finalReply: true },
    );
  }
  const activity = activityOf(turn, phase === "running" ? "running" : "completed", { endReason: phase === "running" ? null : "stop" });
  return {
    title: "测试对话",
    messages: [
      { id: "user-alternating", role: "user", parts: [{ type: "text", text: "把调度 tick 改成可配置，并补测试", status: "completed" }], turn, inherited: false, outcome: null, source: null },
      { id: `assistant-turn-${turn}`, role: "assistant", parts, turn, inherited: false, outcome: phase === "running" ? null : { ...outcomeOf(activity), durationMs: 192_000 }, source: null },
    ],
    activity,
  };
}

function webSearchState() {
  return completedFixture(78, {
    id: "user-web-search",
    role: "user",
    parts: [{ type: "text", text: "搜索 assistant-ui", status: "completed" }],
  }, [
    {presentation: "activity",
      type: "tool-call", awaitingUser: false,
      toolCallId: "trace-web-search",
      toolName: "web_search",
      args: { query: "assistant-ui" },
      status: "completed",
      error: null,
      display_data: {
        kind: "web-search-results",
        query: "assistant-ui",
        results: [{ title: "Assistant UI 官方文档", url: "https://assistant-ui.com/docs" }],
      },
      isError: false,
    },
    { type: "text", text: "搜索完成", status: "completed" },
  ]);
}

const mermaidSequenceDiagram = [
  "```mermaid",
  "sequenceDiagram",
  "    participant C as 客户端",
  "    participant S as 服务端",
  "    C->>S: 请求数据",
  "    S-->>C: 返回数据",
  "```",
].join("\n");

function mermaidState() {
  return stateWithExchange(emptyState(), "查看时序图", 79, mermaidSequenceDiagram, "completed");
}

function delegationState() {
  return completedFixture(500, {
    id: "user-delegation",
    role: "user",
    parts: [{ type: "text", text: "请委派一个审查任务", status: "completed" }],
  }, [
    {presentation: "standalone",
      type: "tool-call", awaitingUser: false,
      toolCallId: "delegate-ref-500",
      toolName: "delegate_task",
      args: { child_agent_id: "delegate_reviewer", agent_name: "审查代码", message: "请审查当前改动" },
      status: "completed",
      display_data: { kind: "delegation-result", title: "审查代码", role: "Reviewer", child_task_id: 501 },
      child_task_id: 501,
      agent_role: "Reviewer",
      isError: false,
    },
    {presentation: "standalone",
      type: "tool-call", awaitingUser: false,
      toolCallId: "delegate-ref-501",
      toolName: "delegate_task",
      args: { child_agent_id: "delegate_tester", agent_name: "检查测试", message: "请检查相关测试" },
      status: "completed",
      display_data: { kind: "delegation-result", title: "检查测试", role: "Tester", child_task_id: 502 },
      child_task_id: 502,
      agent_role: "Tester",
      isError: false,
    },
    { type: "text", text: "子 Agent 已开始工作。", status: "completed" },
  ]);
}

function childDelegationState() {
  return stateWithExchange(emptyState(), "请审查当前改动", 501, "", "running");
}

function secondChildDelegationState() {
  return stateWithExchange(emptyState(), "请检查相关测试", 502, "", "running");
}

function jsonResponse(res, status, value) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
  });
  res.end(JSON.stringify(value));
}

/** 用户命令处理完即 200 `{}`，输出经 Task 长订阅到达；取消是纯投递（202）。 */
const accepted = (res, status = 200) => jsonResponse(res, status, {});

function writeSse(res, frame) {
  res.write(`data: ${JSON.stringify(frame)}\n\n`);
}

/** 与后端 `Constant.Transport.HISTORY_WINDOW_MESSAGES` 一致。 */
const HISTORY_WINDOW_MESSAGES = 100;

/** 与后端 `snapshot_window._turn_start` 一致：起点往前对齐到一条用户消息。 */
function turnStart(messages, start) {
  if (start <= 0) return 0;
  for (let index = start; index >= 0; index -= 1) if (messages[index].role === "user") return index;
  return start;
}

/**
 * wire 上的 Task state：模拟后端把进程内终端会话与后台工作看板投影进 snapshot 顶层，并像后端一样
 * 只带最近一段消息（`messageOffset` 为前面没带的条数）。这里保存的始终是完整历史。
 */
function wireState(taskId, state = states.get(taskId)) {
  const start = turnStart(state.messages, state.messages.length - HISTORY_WINDOW_MESSAGES);
  return {
    ...state,
    messages: state.messages.slice(start),
    messageOffset: start,
    terminal_sessions: (terminalSessions.get(taskId) ?? []).map((session) => session.entry),
    background_tasks: backgroundTasks.get(taskId) ?? [],
    foreground_waits: (foregroundWaits.get(taskId) ?? []).map((entry) => entry.toolCallId),
    handoffs: [],
  };
}

/** 向 Task 的全部订阅推送一帧，并记入最近一轮的流日志。 */
function publish(taskId, frame) {
  const encoded = `data: ${JSON.stringify(frame)}\n\n`;
  lastStreamBody += encoded;
  for (const res of subscribers.get(taskId) ?? []) res.write(encoded);
}

/** 落定新的 Task 状态：结构变化以 full 帧推送（前端对 full 与 mutation 同一语义收敛）。 */
function commit(taskId, state) {
  states.set(taskId, state);
  publish(taskId, { task_id: taskId, kind: "full", mutations: [], state: wireState(taskId) });
  publishTaskStatus(taskId);
}

/** 模拟后端 `/task-status/stream` 的订阅者；与后端一样，Task 的执行状态取当前活动的状态。 */
const statusSubscribers = new Set();
const executionStatus = (taskId) => states.get(taskId)?.activity?.status ?? null;

function writeStatusFrame(res, frame) {
  res.write(`${JSON.stringify(frame)}\n`);
}

/** 只在状态真的变了时推送，与后端「Run 开始或状态迁移才发布」一致。 */
const lastPublishedStatus = new Map();
function publishTaskStatus(taskId) {
  const status = executionStatus(taskId);
  if (lastPublishedStatus.has(taskId) && lastPublishedStatus.get(taskId) === status) return;
  lastPublishedStatus.set(taskId, status);
  for (const res of statusSubscribers) writeStatusFrame(res, { type: "changed", statuses: { [taskId]: status } });
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/**
 * 模拟一次 Run 执行：先落定运行中状态，再按块推 append-text 增量，最后收敛到终态。
 * 执行期间收到取消就把活动收敛为 cancelled；`drop` 时同时断开订阅，模拟连接中途掉线。
 */
async function execute(taskId, { initialState, chunks, finalState }) {
  const generation = testGeneration;
  const executor = { cancelled: false, drop: false };
  executors.set(taskId, executor);
  lastStreamBody = "";
  commit(taskId, initialState);
  const assistantIndex = initialState.messages.length - 1;
  const stillCurrent = () => generation === testGeneration && executors.get(taskId) === executor;
  const finishCancelled = () => {
    executors.delete(taskId);
    const cancelled = settle(states.get(taskId), "cancelled");
    if (executor.drop) {
      states.set(taskId, cancelled);
      publishTaskStatus(taskId);
      for (const res of subscribers.get(taskId) ?? []) res.destroy();
      return;
    }
    commit(taskId, cancelled);
  };
  await wait(120);
  for (const chunk of chunks) {
    if (!stillCurrent()) return;
    if (executor.cancelled) return finishCancelled();
    const state = structuredClone(states.get(taskId));
    const parts = state.messages[assistantIndex].parts;
    const mutations = [];
    const textIndex = parts.findIndex((part) => part.type === "text");
    parts[textIndex].text += chunk;
    mutations.push({ kind: "append-text", path: ["messages", assistantIndex, "parts", textIndex, "text"], value: chunk });
    states.set(taskId, state);
    publish(taskId, { task_id: taskId, kind: "mutation", mutations });
    await wait(420);
  }
  if (!stillCurrent()) return;
  if (executor.cancelled) return finishCancelled();
  executors.delete(taskId);
  commit(taskId, finalState);
  telemetry.completedStreamCount += 1;
}

async function executeToolLifecycle(taskId, initialState, finalState) {
  const generation = testGeneration;
  executors.set(taskId, { cancelled: false, drop: false });
  lastStreamBody = "";
  commit(taskId, initialState);
  await wait(250);
  if (generation !== testGeneration) return;
  const running = structuredClone(initialState);
  running.messages.at(-1).parts[0].status = "running";
  commit(taskId, running);
  await wait(250);
  if (generation !== testGeneration) return;
  executors.delete(taskId);
  commit(taskId, finalState);
  telemetry.completedStreamCount += 1;
}

/** 从运行中或已停下的 Run 继续：同一个 Run 回到 running，当前回合的回复续写为 resumed response。 */
function continueRun(taskId) {
  const previous = states.get(taskId);
  const initialState = structuredClone(previous);
  initialState.activity = { ...initialState.activity, status: "running", endReason: null, resumable: false };
  const assistant = [...initialState.messages].reverse().find((message) => message.role === "assistant");
  assistant.outcome = null;
  assistant.parts = textMessage(assistant.id, "assistant", "", "running", assistant.turn).parts;
  const finalState = settle(initialState, "completed");
  finalState.messages[initialState.messages.indexOf(assistant)].parts = textMessage(assistant.id, "assistant", "resumed response", "completed", assistant.turn).parts;
  finalState.messages[initialState.messages.indexOf(assistant)].outcome = outcomeOf(finalState.activity);
  void execute(taskId, { initialState, chunks: ["resumed", " response"], finalState });
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.setEncoding("utf8");
    req.on("data", (chunk) => {
      body += chunk;
    });
    req.on("end", () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (error) {
        reject(error);
      }
    });
    req.on("error", reject);
  });
}

function commandText(body) {
  return body?.message?.parts
    ?.filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n") ?? "";
}

/**
 * 模拟 POST /tasks/{id}/messages：受理即 200 `{}`，本轮输出由模拟执行者推给 Task 长订阅；与生产「命令同步
 * 受理 + 常驻订阅」的语义一致。运行中发送（生产里排队）也开新一轮，测试只关心命令与订阅的边界。
 */
function handleMessage(res, body, taskId) {
  telemetry.requestBodies.push({ ...body, taskId });
  const text = commandText(body);
  if (!text.trim()) {
    jsonResponse(res, 400, { detail: { error: { code: "INVALID_MESSAGE", message: "message is required", retryable: false } } });
    return;
  }

  const turn = nextTurn++;
  // 与后端一致：本轮冻结请求携带的发起配置，快照 activity.launch 按它投影。
  const launch = {
    modelConfigId: body.modelConfigId ?? DEFAULT_LAUNCH.modelConfigId,
    reasoningEffort: body.reasoningEffort ?? null,
    disabledToolGroups: body.disabledToolGroups ?? [],
  };
  const base = states.get(taskId) ?? emptyState();
  accepted(res);

  if (text.startsWith("tool-lifecycle")) {
    const finalStatus = text.startsWith("tool-lifecycle-failed") ? "failed"
      : text.startsWith("tool-lifecycle-cancelled") ? "cancelled"
        : "completed";
    const initialState = stateWithExchange(base, text, turn, "", "running");
    initialState.messages.at(-1).parts = toolLifecycleParts(turn, "running", "pending");
    const finalState = stateWithExchange(base, text, turn, "", finalStatus);
    finalState.messages.at(-1).parts = toolLifecycleParts(turn, finalStatus, finalStatus);
    for (const state of [initialState, finalState]) state.activity.launch = launch;
    void executeToolLifecycle(taskId, initialState, finalState);
    return;
  }

  const initialState = stateWithExchange(base, text, turn, "", "running");
  const finalState = stateWithExchange(base, text, turn, "streaming response", "completed");
  for (const state of [initialState, finalState]) state.activity.launch = launch;
  if (text.startsWith("usage-regression")) {
    const usage = usageFixture(text.includes("second"));
    for (const state of [initialState, finalState]) {
      state.activity.usage = usage;
      state.activity.contextWindowTotal = 100_000;
    }
    finalState.messages.at(-1).outcome = outcomeOf(finalState.activity);
  }
  void execute(taskId, { initialState, chunks: ["stream", "ing response"], finalState });
}

/**
 * 模拟 POST /tasks/{id}/messages/{messageId}/withdraw：与后端一致，只在 Task 停下时撤回最新一条用户消息
 * 开启的回合（移出它及之后的消息），不开新 Run；运行中 409 `RUN_NOT_STOPPED`，不是最新一条 409
 * `WITHDRAW_TARGET_STALE`。正文为 `withdraw-failure` 的消息模拟后端拒绝。
 */
function handleWithdraw(res, taskId, messageId) {
  telemetry.requestBodies.push({ taskId, withdrawnMessageId: messageId });
  const state = states.get(taskId) ?? emptyState();
  if (executors.has(taskId) || ["running", "suspended"].includes(state.activity?.status)) {
    jsonResponse(res, 409, { detail: { error: { code: "RUN_NOT_STOPPED", message: "当前运行尚未停止，请停止后再撤回", retryable: true } } });
    return;
  }
  const index = state.messages.findIndex((message) => message.id === messageId);
  const latest = state.messages.findLastIndex((message) => message.role === "user");
  const target = state.messages[index];
  const failure = target?.parts.some((part) => part.type === "text" && part.text === "withdraw-failure");
  if (index < 0 || index !== latest || failure) {
    jsonResponse(res, 409, { detail: { error: { code: "WITHDRAW_TARGET_STALE", message: "只能撤回最新一条用户消息，请刷新后重试", retryable: false } } });
    return;
  }
  const next = structuredClone(state);
  next.messages = next.messages.slice(0, index);
  // 与后端一致：最后一个回合被撤回的 Run 不可恢复。
  if (next.activity) next.activity = { ...next.activity, resumable: false };
  jsonResponse(res, 200, {});
  commit(taskId, next);
}

/** 模拟 POST /tasks/{id}/resume：按快照的 `activity.resumable` 判定；不可恢复时 409 `RUN_NOT_RESUMABLE`。 */
function handleBusinessResume(res, taskId) {
  const activity = states.get(taskId)?.activity;
  if (!activity?.resumable || executors.has(taskId)) {
    jsonResponse(res, 409, { detail: { error: { code: "RUN_NOT_RESUMABLE", message: "当前运行不可恢复", retryable: true } } });
    return;
  }
  accepted(res);
  continueRun(taskId);
}

function handleCancel(res, taskId) {
  accepted(res, 202);
  const executor = executors.get(taskId);
  if (executor) {
    executor.cancelled = true;
    executor.drop = dropNextCancelledStream;
    dropNextCancelledStream = false;
    return;
  }
  // 挂起中的 Run 没有执行者：取消由调度直接迁移为 cancelled。
  const state = states.get(taskId);
  if (state?.activity?.status === "suspended") commit(taskId, settle(state, "cancelled"));
}

/** Task 长订阅：先推 full 帧，之后持续推送；没有 Run 也保持连接，只在客户端断开时结束。 */
function handleAttach(res, taskId) {
  if (!states.has(taskId)) {
    jsonResponse(res, 404, { detail: { error: { code: "TASK_NOT_FOUND", message: "任务不存在", retryable: false } } });
    return;
  }
  res.writeHead(200, {
    "X-Cosir-Task-Id": String(taskId),
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "Content-Type": "text/event-stream; charset=utf-8",
  });
  const set = subscribers.get(taskId) ?? new Set();
  subscribers.set(taskId, set);
  set.add(res);
  res.on("close", () => set.delete(res));
  writeSse(res, { task_id: taskId, kind: "full", mutations: [], state: wireState(taskId) });
  // seeded 的运行中 Run 没有执行者：首个订阅到来时模拟后端继续执行它。
  if (isActive(states.get(taskId).activity?.status) && states.get(taskId).activity.status === "running" && !executors.has(taskId)) {
    continueRun(taskId);
  }
}

/** 编辑排队消息：原地改正文，消息已并入时 409 MESSAGE_CONSUMED。 */
/**
 * 模拟排队消息的立即发送与删除（编辑也走删除）：与后端一致，立即发送撤回原条目、以新身份按
 * `immediate` 重新入队；删除只撤回。条目已不在队列时 409 `MESSAGE_CONSUMED`。本测试服务不模拟随后的并入。
 */
function handleQueuedWithdraw(res, taskId, messageId, sendNow) {
  telemetry.requestBodies.push({ taskId, queuedMessageId: messageId, operation: sendNow ? "send_now" : "delete" });
  const state = states.get(taskId);
  const queued = state?.queued_messages?.find((message) => message.id === messageId);
  if (!queued) {
    jsonResponse(res, 409, { detail: { error: { code: "MESSAGE_CONSUMED", message: "消息已被处理", retryable: false } } });
    return;
  }
  const next = structuredClone(state);
  next.queued_messages = next.queued_messages.filter((message) => message.id !== messageId);
  if (sendNow) next.queued_messages.push({ ...queued, id: `${messageId}-now`, delivery: "immediate" });
  jsonResponse(res, 200, {});
  commit(taskId, next);
}

function terminalFrame(session, frame) {
  return JSON.stringify({ protocol: "terminal-v2", session_id: session.entry.sessionId, generation: "e2e", ...frame });
}

function appendTerminalOutput(session, text) {
  const seq = session.output.length + 1;
  const data = Buffer.from(text, "utf8").toString("base64");
  session.output.push({ seq, data });
  for (const socket of session.sockets) socket.send(terminalFrame(session, { type: "output", seq, data_base64: data }));
}

function createTerminalSession(taskId, shell) {
  const sessionId = `term-${nextTerminalId++}`;
  const session = {
    taskId,
    entry: {
      sessionId,
      label: "",
      shellKind: shell === "auto" ? "zsh" : shell,
      createdBy: "user",
      status: "running",
      endReason: null,
      exitCode: null,
      integration: "ready",
      commandRunning: false,
      createdAt: new Date().toISOString(),
    },
    output: [],
    sockets: new Set(),
    rows: 24,
    columns: 80,
  };
  terminalSessions.set(taskId, [...(terminalSessions.get(taskId) ?? []), session]);
  appendTerminalOutput(session, `${sessionId} $ `);
  return session;
}

function findTerminalSession(taskId, sessionId) {
  return (terminalSessions.get(taskId) ?? []).find((session) => session.entry.sessionId === sessionId);
}

/** 与后端 terminal_stream_api 一致：未知会话照常 accept，收到 attach 后发 protocol_error 并以 1008 关闭。 */
function rejectMissingTerminalSession(socket, sessionId) {
  socket.once("message", () => {
    socket.send(JSON.stringify({ protocol: "terminal-v2", session_id: sessionId, type: "protocol_error", code: "TERMINAL_SESSION_NOT_FOUND", message: "terminal session not found" }));
    socket.close(1008, "invalid terminal stream protocol");
  });
}

/** 按 terminal-v2 处理一条 WS：首帧 attach 后续传缓冲；input 原样回显，回车换行并补提示符。 */
function handleTerminalSocket(socket, session) {
  let attached = false;
  socket.on("message", (raw) => {
    const frame = JSON.parse(String(raw));
    if (!attached) {
      if (frame.type !== "attach") {
        socket.send(terminalFrame(session, { type: "protocol_error", code: "TERMINAL_STREAM_PROTOCOL_ERROR", message: "attach must be first" }));
        socket.close();
        return;
      }
      attached = true;
      const afterSeq = frame.after_seq ?? 0;
      socket.send(terminalFrame(session, {
        type: "attached", rows: session.rows, columns: session.columns, status: session.entry.status,
        first_available_seq: 1, next_seq: session.output.length + 1,
      }));
      for (const chunk of session.output) {
        if (chunk.seq > afterSeq) socket.send(terminalFrame(session, { type: "output", seq: chunk.seq, data_base64: chunk.data }));
      }
      if (session.entry.status !== "running") {
        socket.send(terminalFrame(session, { type: "exit" }));
        return;
      }
      session.sockets.add(socket);
      socket.on("close", () => session.sockets.delete(socket));
      return;
    }
    if (frame.type === "input") {
      const text = Buffer.from(frame.data_base64, "base64").toString("utf8");
      appendTerminalOutput(session, text.replace(/\r/g, `\r\n${session.entry.sessionId} $ `));
    } else if (frame.type === "resize") {
      session.rows = frame.rows;
      session.columns = frame.cols;
      socket.send(terminalFrame(session, { type: "geometry", rows: frame.rows, columns: frame.cols }));
    }
  });
}

const server = createServer(async (req, res) => {
  // CORS 头只在这里设一次（writeHead 会合并）；回写请求来源，不绑定某个 Vite 端口，并行运行各用各的。
  res.setHeader("Access-Control-Allow-Origin", req.headers.origin ?? "*");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Trace-Id");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS");
  res.setHeader("Access-Control-Expose-Headers", "X-Cosir-Task-Id");

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url ?? "/", `http://${HOST}:${PORT}`);

  if (req.method === "GET" && url.pathname === "/task-status/stream") {
    res.writeHead(200, { "Content-Type": "application/x-ndjson", "Cache-Control": "no-store" });
    const statuses = Object.fromEntries([...states.keys()].map((taskId) => [taskId, executionStatus(taskId)]));
    for (const [taskId, status] of Object.entries(statuses)) lastPublishedStatus.set(Number(taskId), status);
    writeStatusFrame(res, { type: "full", statuses });
    statusSubscribers.add(res);
    const heartbeat = setInterval(() => writeStatusFrame(res, { type: "heartbeat" }), 2000);
    res.on("close", () => {
      clearInterval(heartbeat);
      statusSubscribers.delete(res);
    });
    return;
  }
  if (req.method === "GET" && url.pathname === "/health") {
    jsonResponse(res, 200, { status: "ok" });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/reset") {
    resetTestState();
    jsonResponse(res, 200, { ok: true });
    return;
  }
  if (req.method === "GET" && url.pathname === "/workspaces") {
    jsonResponse(res, 200, workspaces);
    return;
  }
  if (req.method === "GET" && url.pathname === `/workspaces/${WORKSPACE_ID}/tasks`) {
    jsonResponse(res, 200, [...tasks.values()].filter(task => task.task_type !== "delegate_task"));
    return;
  }
  if (req.method === "POST" && url.pathname === `/workspaces/${WORKSPACE_ID}/tasks`) {
    const body = await readJson(req);
    const text = typeof body.text === "string" ? body.text : "";
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: text.slice(0, 40),
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, emptyState());
    publishTaskStatus(TASK_ID);
    jsonResponse(res, 200, tasks.get(TASK_ID));
    return;
  }
  if (req.method === "DELETE" && /^\/tasks\/\d+$/.test(url.pathname)) {
    const taskId = Number(url.pathname.split("/")[2]);
    tasks.delete(taskId);
    states.delete(taskId);
    executors.delete(taskId);
    // Task 删除结束它的长订阅。
    for (const subscriber of subscribers.get(taskId) ?? []) subscriber.end();
    subscribers.delete(taskId);
    jsonResponse(res, 200, { task_id: taskId, deleted: true, deleted_task_ids: [taskId] });
    return;
  }
  if (req.method === "GET" && url.pathname === "/model-configs") {
    jsonResponse(res, 200, [{
      config_id: 2,
      config_name: "Demo",
      base_url: "https://api.example.com",
      api_key: "test-api-key",
      model_name: "demo-model",
      context_window_k: 128,
      api_key_configured: true,
      enabled: true,
      sort_order: 0,
      created_at: "",
      updated_at: "",
      supports_thinking: false,
      supports_image: false,
      supports_reasoning_effort: false,
    }]);
    return;
  }
  if (req.method === "GET" && url.pathname === "/tools/groups") {
    jsonResponse(res, 200, {
      groups: [
        { group: "文件", tools: [{ name: "read_file", description: "读取文件" }] },
        { group: "交互终端工具", tools: [{ name: "terminal_read", description: "读取终端输出" }] },
        { group: "子Agent工具", tools: [{ name: "delegate_task", description: "委派子任务" }] },
      ],
    });
    return;
  }
  if (req.method === "GET" && url.pathname === "/__test__/last-stream") {
    res.writeHead(200, {
      "Content-Type": "text/plain; charset=utf-8",
    });
    res.end(lastStreamBody);
    return;
  }
  if (req.method === "GET" && url.pathname === "/__test__/telemetry") {
    jsonResponse(res, 200, telemetry);
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-task") {
    const body = await readJson(req);
    const taskId = Number(body.taskId);
    const title = typeof body.title === "string" ? body.title : `任务 ${taskId}`;
    tasks.set(taskId, {
      task_id: taskId,
      workspace_id: WORKSPACE_ID,
      title,
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
      ...(body.taskType ? { task_type: body.taskType } : {}),
      ...(body.extra ? { extra: body.extra } : {}),
    });
    states.set(taskId, emptyState());
    publishTaskStatus(taskId);
    jsonResponse(res, 200, { task_id: taskId });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-state") {
    // 直接落定某个 Task 的 canonical state：state 读取与 attach 首帧共用这一份事实。
    const body = await readJson(req);
    executors.delete(Number(body.taskId));
    if (body.hold) executors.set(Number(body.taskId), { cancelled: false, drop: false });
    commit(Number(body.taskId), body.state);
    jsonResponse(res, 200, { ok: true });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/append-text") {
    // 精确驱动一帧正文增量：用于验证更新隔离与流式滚动，不用 full 帧替代 mutation。
    const { taskId, messageIndex, partIndex, text } = await readJson(req);
    const state = states.get(Number(taskId));
    state.messages[messageIndex].parts[partIndex].text += text;
    publish(Number(taskId), { task_id: Number(taskId), kind: "mutation", mutations: [
      { kind: "append-text", path: ["messages", messageIndex, "parts", partIndex, "text"], value: text },
    ] });
    jsonResponse(res, 200, { ok: true });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-running") {
    const body = await readJson(req);
    const text = typeof body.text === "string" ? body.text : "恢复中的对话";
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: text.slice(0, 40),
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, stateWithExchange(emptyState(), text, 899, "", "running"));
    publishTaskStatus(TASK_ID);
    executors.delete(TASK_ID);
    jsonResponse(res, 200, { task_id: TASK_ID });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-tool-trace") {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "工具追踪视觉回归",
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, toolTraceState());
    publishTaskStatus(TASK_ID);
    jsonResponse(res, 200, { task_id: TASK_ID });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-alternating") {
    const body = await readJson(req);
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "交替序列回合",
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, alternatingSequenceState(body.phase === "running" ? "running" : "settled"));
    publishTaskStatus(TASK_ID);
    // 运行中的样子要稳定：登记一个不推进的执行者，attach 时不会把它当作遗留 Run 续跑成 resumed response。
    if (body.phase === "running") executors.set(TASK_ID, { cancelled: false, drop: false });
    else executors.delete(TASK_ID);
    jsonResponse(res, 200, { task_id: TASK_ID });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-web-search") {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "网页搜索标题展示回归",
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, webSearchState());
    publishTaskStatus(TASK_ID);
    jsonResponse(res, 200, { task_id: TASK_ID });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-mermaid") {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "Mermaid 全屏回归",
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, mermaidState());
    publishTaskStatus(TASK_ID);
    jsonResponse(res, 200, { task_id: TASK_ID });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/drop-next-cancel-stream") {
    dropNextCancelledStream = true;
    jsonResponse(res, 200, { enabled: true });
    return;
  }
  // 模拟后端重启：进程内终端会话全部消失、连接断开，但 snapshot 尚未回推（由 sync 单独触发）。
  if (req.method === "POST" && url.pathname === "/__test__/terminal/expire") {
    const body = await readJson(req);
    for (const session of terminalSessions.get(body.taskId) ?? []) for (const socket of session.sockets) socket.terminate();
    terminalSessions.delete(body.taskId);
    jsonResponse(res, 200, {});
    return;
  }
  // 模拟 shell 集成报告命令开始 / 返回：改 commandRunning 并回推 snapshot。
  if (req.method === "POST" && url.pathname === "/__test__/terminal/command") {
    const body = await readJson(req);
    findTerminalSession(body.taskId, body.sessionId).entry.commandRunning = body.running;
    if (states.has(body.taskId)) commit(body.taskId, states.get(body.taskId));
    jsonResponse(res, 200, {});
    return;
  }
  // 模拟 shell 自己退出（含非 0 退出码）：会话进入 exited，连接收到 exit 帧。
  if (req.method === "POST" && url.pathname === "/__test__/terminal/exit") {
    const body = await readJson(req);
    const session = findTerminalSession(body.taskId, body.sessionId);
    session.entry = {
      ...session.entry, status: "exited", endReason: "shell_exited", exitCode: body.exitCode,
      commandRunning: false,
    };
    for (const socket of session.sockets) socket.send(terminalFrame(session, { type: "exit" }));
    if (states.has(body.taskId)) commit(body.taskId, states.get(body.taskId));
    jsonResponse(res, 200, {});
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/terminal/sync") {
    const body = await readJson(req);
    if (states.has(body.taskId)) commit(body.taskId, states.get(body.taskId));
    jsonResponse(res, 200, {});
    return;
  }
  // 下一次到达的 attach（不分 Task）返回 503。开发模式 StrictMode 挂载时会多发一次随即被中止的
  // attach，挂载前注入会被那次请求吃掉、重连路径根本不走；必须等订阅建立（前端「订阅已建立」日志）
  // 之后再注入，配合 drop-subscriptions 触发重连。
  if (req.method === "POST" && url.pathname === "/__test__/fail-next-attach") {
    failNextAttach = true;
    jsonResponse(res, 200, { enabled: true });
    return;
  }
  // 模拟后端连接中断：断开该 Task 的全部长订阅（不改 state、不结束 Run），前端应进入有限重连。
  if (req.method === "POST" && url.pathname === "/__test__/drop-subscriptions") {
    const body = await readJson(req);
    for (const subscriber of subscribers.get(body.taskId) ?? []) subscriber.destroy();
    jsonResponse(res, 200, {});
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-delegation") {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "委派主任务",
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, delegationState());
    publishTaskStatus(TASK_ID);
    for (const id of [501, 502]) tasks.set(id, {
      task_id: id, workspace_id: WORKSPACE_ID, title: `子任务${id}`, task_type: "agent_task",
      created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(501, childDelegationState());
    publishTaskStatus(501);
    states.set(502, secondChildDelegationState());
    publishTaskStatus(502);
    jsonResponse(res, 200, { task_id: TASK_ID, child_task_id: 501, second_child_task_id: 502 });
    return;
  }
  if (req.method === "GET" && url.pathname === "/terminal/shells") {
    jsonResponse(res, 200, { shells: terminalShells });
    return;
  }
  const terminalCreateRoute = url.pathname.match(/^\/tasks\/(\d+)\/terminal\/sessions$/);
  if (req.method === "POST" && terminalCreateRoute) {
    const taskId = Number(terminalCreateRoute[1]);
    const body = await readJson(req);
    const session = createTerminalSession(taskId, typeof body.shell === "string" ? body.shell : "auto");
    jsonResponse(res, 200, { sessionId: session.entry.sessionId });
    if (states.has(taskId)) commit(taskId, states.get(taskId));
    return;
  }
  const terminalSessionRoute = url.pathname.match(/^\/tasks\/(\d+)\/terminal\/sessions\/([^/]+)$/);
  if (req.method === "DELETE" && terminalSessionRoute) {
    const taskId = Number(terminalSessionRoute[1]);
    const session = findTerminalSession(taskId, decodeURIComponent(terminalSessionRoute[2]));
    if (!session) {
      jsonResponse(res, 404, { detail: { code: "TERMINAL_SESSION_NOT_FOUND", message: "终端会话不存在", retryable: false } });
      return;
    }
    session.entry = { ...session.entry, status: "closed", endReason: "user_closed", exitCode: null };
    for (const socket of session.sockets) {
      socket.send(terminalFrame(session, { type: "exit" }));
    }
    jsonResponse(res, 200, {});
    if (states.has(taskId)) commit(taskId, states.get(taskId));
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-background-tasks") {
    // 模拟后端登记表：种入看板条目与前台等待，经 full 帧推送（前端按 snapshot 收敛）。
    const body = await readJson(req);
    const taskId = Number(body.taskId);
    backgroundTasks.set(taskId, body.entries ?? []);
    foregroundWaits.set(taskId, body.foregroundWaits ?? []);
    if (states.has(taskId)) commit(taskId, states.get(taskId));
    jsonResponse(res, 200, { ok: true });
    return;
  }
  const backgroundRoute = url.pathname.match(/^\/tasks\/(\d+)\/tool-calls\/([^/]+)\/(cancel|background)$/);
  if (req.method === "POST" && backgroundRoute) {
    // 与后端一致：登记过的调用按 tool_call_id 撤销或转后台；后台占位随撤销推到 cancelled。
    const taskId = Number(backgroundRoute[1]);
    const toolCallId = decodeURIComponent(backgroundRoute[2]);
    const entries = backgroundTasks.get(taskId) ?? [];
    const waits = foregroundWaits.get(taskId) ?? [];
    const detach = backgroundRoute[3] === "background";
    const target = detach
      ? waits.find((entry) => entry.toolCallId === toolCallId)
      : entries.find((entry) => entry.toolCallId === toolCallId && entry.status === "running");
    if (!target) {
      jsonResponse(res, 409, { error: { code: "TOOL_CALL_NOT_DETACHABLE", message: "这次工具调用已结束或不能转到后台", retryable: false } });
      return;
    }
    if (detach) {
      foregroundWaits.set(taskId, waits.filter((entry) => entry !== target));
      backgroundTasks.set(taskId, [target, ...entries]);
    } else {
      backgroundTasks.set(taskId, entries.map((entry) => entry !== target ? entry
        : { ...entry, status: "cancelled", endedAt: new Date().toISOString() }));
    }
    const state = states.get(taskId);
    if (!detach && state) {
      const messages = state.messages.map((message) => ({
        ...message,
        parts: message.parts.map((part) => part.type === "tool-call" && part.toolCallId === toolCallId && part.background
          ? { ...part, status: "cancelled", error: "已取消" } : part),
      }));
      states.set(taskId, { ...state, messages });
      publishTaskStatus(taskId);
    }
    if (states.has(taskId)) commit(taskId, states.get(taskId));
    jsonResponse(res, 200, {});
    return;
  }
  if (req.method === "POST" && /^\/tasks\/\d+\/cancel$/.test(url.pathname)) {
    // 与后端一致按 Task 取消：只投递停止信号，终态经长订阅到达。
    handleCancel(res, Number(url.pathname.split("/")[2]));
    return;
  }
  const queuedRoute = url.pathname.match(/^\/tasks\/(\d+)\/queued-messages\/([^/]+)$/);
  if (req.method === "DELETE" && queuedRoute) {
    handleQueuedWithdraw(res, Number(queuedRoute[1]), decodeURIComponent(queuedRoute[2]), false);
    return;
  }
  const sendNowRoute = url.pathname.match(/^\/tasks\/(\d+)\/queued-messages\/([^/]+)\/send-now$/);
  if (req.method === "POST" && sendNowRoute) {
    handleQueuedWithdraw(res, Number(sendNowRoute[1]), decodeURIComponent(sendNowRoute[2]), true);
    return;
  }
  if (req.method === "GET" && /^\/tasks\/\d+$/.test(url.pathname)) {
    const taskId = Number(url.pathname.split("/")[2]);
    const task = tasks.get(taskId);
    if (!task) {
      jsonResponse(res, 404, { detail: "task not found" });
      return;
    }
    jsonResponse(res, 200, { ...task, task_type: task.task_type ?? "user" });
    return;
  }
  if (req.method === "GET" && url.pathname.startsWith("/tasks/") && url.pathname.endsWith("/assistant/state")) {
    const taskId = Number(url.pathname.split("/")[2]);
    if (!states.has(taskId)) {
      jsonResponse(res, 404, { detail: "task not found" });
      return;
    }
    jsonResponse(res, 200, wireState(taskId));
    return;
  }
  const historyRoute = url.pathname.match(/^\/tasks\/(\d+)\/assistant\/messages$/);
  if (req.method === "GET" && historyRoute) {
    // 与后端 `history_page` 一致：完整历史中 end 之前的一页，越界（快照已被替换）时 409。
    const messages = states.get(Number(historyRoute[1]))?.messages ?? [];
    const end = Number(url.searchParams.get("end"));
    if (end > messages.length) {
      jsonResponse(res, 409, { detail: { error: { code: "HISTORY_STALE", message: "对话已更新，请重新加载更早的消息", retryable: false } } });
      return;
    }
    const start = turnStart(messages, end - HISTORY_WINDOW_MESSAGES);
    jsonResponse(res, 200, { messages: messages.slice(start, end), messageOffset: start });
    return;
  }
  const withdrawRoute = url.pathname.match(/^\/tasks\/(\d+)\/messages\/([^/]+)\/withdraw$/);
  if (req.method === "POST" && withdrawRoute) {
    handleWithdraw(res, Number(withdrawRoute[1]), decodeURIComponent(withdrawRoute[2]));
    return;
  }
  const taskRoute = url.pathname.match(/^\/tasks\/(\d+)\/(messages|resume)$/);
  if (req.method === "POST" && taskRoute) {
    const routeTaskId = Number(taskRoute[1]);
    try {
      if (taskRoute[2] === "resume") handleBusinessResume(res, routeTaskId);
      else handleMessage(res, await readJson(req), routeTaskId);
    } catch {
      jsonResponse(res, 400, { error: { code: "INVALID_REQUEST", message: "invalid request", retryable: false } });
    }
    return;
  }
  if (req.method === "POST" && /^\/tasks\/\d+\/assistant\/attach$/.test(url.pathname)) {
    if (failNextAttach) {
      failNextAttach = false;
      jsonResponse(res, 503, { detail: { error: { code: "ATTACH_TEMPORARY_FAILURE", message: "attach temporarily unavailable", retryable: true } } });
      return;
    }
    handleAttach(res, Number(url.pathname.split("/")[2]));
    return;
  }
  jsonResponse(res, 404, { detail: "not found" });
});

const terminalSockets = new WebSocketServer({ noServer: true });
server.on("upgrade", (req, socket, head) => {
  const url = new URL(req.url ?? "/", `http://${HOST}:${PORT}`);
  const route = url.pathname.match(/^\/tasks\/(\d+)\/terminal\/sessions\/([^/]+)\/stream$/);
  if (!route) {
    socket.destroy();
    return;
  }
  const sessionId = decodeURIComponent(route[2]);
  const session = findTerminalSession(Number(route[1]), sessionId);
  terminalSockets.handleUpgrade(req, socket, head, (ws) => (
    session ? handleTerminalSocket(ws, session) : rejectMissingTerminalSession(ws, sessionId)
  ));
});

server.listen(PORT, HOST, () => {
  console.log(`E2E test service listening on http://${HOST}:${PORT}`);
});

function shutdown() {
  server.closeAllConnections?.();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 250).unref();
}

process.once("SIGINT", shutdown);
process.once("SIGTERM", shutdown);
