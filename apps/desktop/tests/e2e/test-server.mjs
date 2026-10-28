/* global structuredClone, setTimeout, clearTimeout, URL, console, process, Buffer */

import { createServer } from "node:http";
import { WebSocketServer } from "ws";

const HOST = "127.0.0.1";
const PORT = 8000;
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
let nextRunId = 1;
let testGeneration = 0;
let dropNextCancelledStream = false;
let failNextAttach = false;
/**
 * 交互终端模拟：taskId -> 会话数组。每个会话持有 snapshot 条目、输出缓冲（seq 递增）与已 attach 的 WS。
 * 只模拟 terminal-v2 的最小闭环（attach 续传、input 回显与短租约、resize、exit），不模拟交接。
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
  clientCancelCount: 0,
  completedStreamCount: 0,
  requestBodies: [],
};

function resetTestState() {
  testGeneration += 1;
  tasks.clear();
  states.clear();
  for (const set of subscribers.values()) for (const res of set) res.end();
  subscribers.clear();
  executors.clear();
  for (const sessions of terminalSessions.values()) {
    for (const session of sessions) {
      clearTimeout(session.leaseTimer);
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
  nextRunId = 1;
  telemetry.clientCancelCount = 0;
  telemetry.completedStreamCount = 0;
  telemetry.requestBodies = [];
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const isActive = (status) => status === "running" || status === "suspended";
// 与 /model-configs 唯一的模型一致：运行期配置锁定下，运行中发送沿用这份配置。
const DEFAULT_LAUNCH = { modelConfigId: 2, reasoningEffort: null, disabledToolGroups: [] };

function taskProjection(task) {
  const activity = states.get(task.task_id)?.activity ?? null;
  return { ...task, execution_status: activity?.status ?? null };
}

const emptyState = () => ({ title: "新对话", messages: [], activity: null });

const activityOf = (runId, status, overrides = {}) => ({
  runId,
  status,
  endReason: status === "completed" ? "stop" : status === "cancelled" ? "user_cancelled" : null,
  error: null,
  usage: null,
  contextWindowTotal: null,
  launch: DEFAULT_LAUNCH,
  ...overrides,
});

const outcomeOf = (activity) => ({
  status: activity.status,
  endReason: activity.endReason,
  error: activity.error,
  usage: activity.usage,
  contextWindowTotal: activity.contextWindowTotal,
});

const textMessage = (id, role, text, status, runId) => ({
  id,
  role,
  parts: role === "assistant"
    ? [
        { type: "reasoning", text: status === "completed" ? "推理完成" : "正在推理", status: status === "completed" ? "completed" : "running" },
        { type: "text", text, status: status === "completed" ? "completed" : "running" },
      ]
    : [{ type: "text", text, status: "completed" }],
  runId,
  outcome: null,
});

/** 一轮问答：用户消息 id 为 `user-{runId}`，回复 id 按契约为 `assistant-{用户消息 id}`。 */
function stateWithExchange(previous, text, runId, assistantText, status) {
  const next = structuredClone(previous);
  const userId = `user-${runId}`;
  next.messages.push(
    textMessage(userId, "user", text, "completed", runId),
    textMessage(`assistant-${userId}`, "assistant", assistantText, status, runId),
  );
  next.activity = activityOf(runId, TERMINAL.has(status) ? status : "running", {
    endReason: status === "completed" ? "stop" : status === "failed" ? "max_steps_reached" : status === "cancelled" ? "user_cancelled" : null,
  });
  if (TERMINAL.has(status)) next.messages.at(-1).outcome = outcomeOf(next.activity);
  return next;
}

/** 把当前活动收敛到终态：结局写到这一轮最后一条助手消息上。 */
function settle(state, status, overrides = {}) {
  const next = structuredClone(state);
  next.activity = { ...next.activity, ...activityOf(next.activity.runId, status), usage: next.activity.usage, contextWindowTotal: next.activity.contextWindowTotal, ...overrides };
  const last = [...next.messages].reverse().find((message) => message.role === "assistant" && message.runId === next.activity.runId);
  if (last) {
    last.outcome = outcomeOf(next.activity);
    for (const part of last.parts) if (part.status === "running") part.status = "completed";
  }
  return next;
}

function toolLifecycleParts(runId, status, toolStatus) {
  return [
    {
      type: "tool-call",
      toolCallId: `tool-lifecycle-${runId}`,
      toolName: "search_content",
      args: { pattern: "lifecycle" },
      status: toolStatus,
      error: null,
      presentation: { verb: "搜索文件", icon: "search", surface: "standalone", expandable: true, expand_layout: "list" },
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

function completedFixture(runId, userMessage, assistantParts) {
  const activity = activityOf(runId, "completed", { launch: { modelConfigId: null, reasoningEffort: null, disabledToolGroups: [] } });
  return {
    title: "测试对话",
    messages: [
      { ...userMessage, runId, outcome: null },
      { id: `assistant-${userMessage.id}`, role: "assistant", parts: assistantParts, runId, outcome: outcomeOf(activity) },
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
    { type: "reasoning", text: "先分析项目结构", status: "completed" },
    {
      type: "tool-call",
      toolCallId: "trace-read-file",
      toolName: "read_file",
      args: { path: "README.md" },
      status: "completed",
      error: null,
      presentation: { verb: "读取文件", icon: "eye", expandable: false, expand_layout: "none" },
      display_data: null,
      isError: false,
    },
    {
      type: "tool-call",
      toolCallId: "trace-search-files",
      toolName: "search_content",
      args: { pattern: "assistant-ui" },
      status: "completed",
      error: null,
      presentation: { verb: "搜索文件", icon: "search", expandable: false, expand_layout: "none" },
      display_data: null,
      isError: false,
    },
    { type: "text", text: "检查完成", status: "completed" },
  ]);
}

function webSearchState() {
  return completedFixture(78, {
    id: "user-web-search",
    role: "user",
    parts: [{ type: "text", text: "搜索 assistant-ui", status: "completed" }],
  }, [
    {
      type: "tool-call",
      toolCallId: "trace-web-search",
      toolName: "web_search",
      args: { query: "assistant-ui" },
      status: "completed",
      error: null,
      presentation: {
        verb: "网页搜索",
        icon: "globe",
        surface: "standalone",
        expandable: true,
        expand_layout: "list",
        default_open: true,
      },
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
    {
      type: "tool-call",
      toolCallId: "delegate-ref-500",
      toolName: "delegate_task",
      args: { child_agent_id: "delegate_reviewer", title: "审查代码", prompt: "请审查当前改动" },
      status: "completed",
      presentation: { verb: "委派 Agent", icon: "users", surface: "standalone", expandable: false, expand_layout: "none" },
      display_data: { kind: "delegation-result", title: "审查代码", role: "Reviewer", child_task_id: 501 },
      child_task_id: 501,
      agent_role: "Reviewer",
      isError: false,
    },
    {
      type: "tool-call",
      toolCallId: "delegate-ref-501",
      toolName: "delegate_task",
      args: { child_agent_id: "delegate_tester", title: "检查测试", prompt: "请检查相关测试" },
      status: "completed",
      presentation: { verb: "委派 Agent", icon: "users", surface: "standalone", expandable: false, expand_layout: "none" },
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
    "Access-Control-Allow-Origin": "http://127.0.0.1:4173",
    "Access-Control-Allow-Headers": "Content-Type, X-Trace-Id",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "X-Cosir-Task-Id",
    "Content-Type": "application/json; charset=utf-8",
  });
  res.end(JSON.stringify(value));
}

/** 用户命令处理完即 200 `{}`，输出经 Task 长订阅到达；取消是纯投递（202）。 */
const accepted = (res, status = 200) => jsonResponse(res, status, {});

function writeSse(res, frame) {
  res.write(`data: ${JSON.stringify(frame)}\n\n`);
}

/** wire 上的 Task state：模拟后端把进程内终端会话与后台工作看板投影进 snapshot 顶层。 */
function wireState(taskId, state = states.get(taskId)) {
  return {
    ...state,
    terminal_sessions: (terminalSessions.get(taskId) ?? []).map((session) => session.entry),
    background_tasks: backgroundTasks.get(taskId) ?? [],
    foreground_waits: (foregroundWaits.get(taskId) ?? []).map((entry) => entry.toolCallId),
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
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/**
 * 模拟一次 Run 执行：先落定运行中状态，再按块推 append-text 增量，最后收敛到终态。
 * 执行期间收到取消就把活动收敛为 cancelled；`drop` 时同时断开订阅，模拟连接中途掉线。
 */
async function execute(taskId, { initialState, chunks, finalState, prefixReasoning = true }) {
  const generation = testGeneration;
  const runId = initialState.activity.runId;
  const executor = { runId, cancelled: false, drop: false };
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
    if (prefixReasoning && parts[0]?.type === "reasoning") {
      parts[0].text += "先分析一下。";
      mutations.push({ kind: "append-text", path: ["messages", assistantIndex, "parts", 0, "text"], value: "先分析一下。" });
    }
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

async function executeToolLifecycle(taskId, runId, initialState, finalState) {
  const generation = testGeneration;
  executors.set(taskId, { runId, cancelled: false, drop: false });
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

/** 从运行中或用户取消的 Run 继续：同一个 Run 回到 running，回复续写为 resumed response。 */
function continueRun(taskId) {
  const previous = states.get(taskId);
  const initialState = structuredClone(previous);
  initialState.activity = { ...initialState.activity, status: "running", endReason: null };
  const assistant = [...initialState.messages].reverse().find((message) => message.role === "assistant");
  assistant.outcome = null;
  assistant.parts = textMessage(assistant.id, "assistant", "", "running", assistant.runId).parts;
  const finalState = settle(initialState, "completed");
  finalState.messages[initialState.messages.indexOf(assistant)].parts = textMessage(assistant.id, "assistant", "resumed response", "completed", assistant.runId).parts;
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
 * 模拟 POST /tasks/{id}/messages 与 /messages/{messageId}/rerun：受理即 200 `{}`，本轮输出由模拟
 * 执行者推给 Task 长订阅；与生产「命令同步受理 + 常驻订阅」的语义一致。运行中发送（immediate）也开新一轮，
 * 测试只关心命令与订阅的边界。
 */
function handleMessage(res, body, taskId, sourceId) {
  telemetry.requestBodies.push({ ...body, taskId, ...(sourceId ? { sourceId } : {}) });
  const text = commandText(body);
  if (!text.trim()) {
    jsonResponse(res, 400, { detail: { error: { code: "INVALID_MESSAGE", message: "message is required", retryable: false } } });
    return;
  }
  if (typeof sourceId === "string" && text === "edit-failure") {
    jsonResponse(res, 400, { detail: { error: { code: "EDIT_REJECTED", message: "编辑重跑被测试后端拒绝", retryable: true } } });
    return;
  }

  const runId = nextRunId++;
  // 与后端一致：本轮冻结请求携带的发起配置，快照 activity.launch 按它投影。
  const launch = {
    modelConfigId: body.modelConfigId ?? DEFAULT_LAUNCH.modelConfigId,
    reasoningEffort: body.reasoningEffort ?? null,
    disabledToolGroups: body.disabledToolGroups ?? [],
  };
  const previous = states.get(taskId) ?? emptyState();
  let base = previous;
  if (typeof sourceId === "string") {
    // 编辑重跑：被编辑的用户消息及其后内容移出投影，编辑后的消息以新消息身份进入新一轮。
    const sourceIndex = previous.messages.findIndex((message) => message.id === sourceId);
    if (sourceIndex >= 0) {
      base = structuredClone(previous);
      base.messages = base.messages.slice(0, sourceIndex);
    }
  }
  accepted(res);

  if (text.startsWith("tool-lifecycle")) {
    const finalStatus = text.startsWith("tool-lifecycle-failed") ? "failed"
      : text.startsWith("tool-lifecycle-cancelled") ? "cancelled"
        : "completed";
    const initialState = stateWithExchange(base, text, runId, "", "running");
    initialState.messages.at(-1).parts = toolLifecycleParts(runId, "running", "pending");
    const finalState = stateWithExchange(base, text, runId, "", finalStatus);
    finalState.messages.at(-1).parts = toolLifecycleParts(runId, finalStatus, finalStatus);
    for (const state of [initialState, finalState]) state.activity.launch = launch;
    void executeToolLifecycle(taskId, runId, initialState, finalState);
    return;
  }

  const initialState = stateWithExchange(base, text, runId, "", "running");
  const finalState = stateWithExchange(base, text, runId, "streaming response", "completed");
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

/** 模拟 POST /tasks/{id}/resume：只续跑用户取消的 Run；不可续跑时 409 `RUN_NOT_RESUMABLE`。 */
function handleBusinessResume(res, taskId) {
  const activity = states.get(taskId)?.activity;
  if (!activity || activity.status !== "cancelled" || activity.endReason !== "user_cancelled" || executors.has(taskId)) {
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
    "Access-Control-Allow-Origin": "http://127.0.0.1:4173",
    "Access-Control-Allow-Headers": "Content-Type, X-Trace-Id",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "X-Cosir-Task-Id",
    "X-Cosir-Task-Id": String(taskId),
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "Content-Type": "text/event-stream; charset=utf-8",
  });
  const set = subscribers.get(taskId) ?? new Set();
  subscribers.set(taskId, set);
  set.add(res);
  const generation = testGeneration;
  res.on("close", () => {
    set.delete(res);
    if (generation === testGeneration && executors.has(taskId)) telemetry.clientCancelCount += 1;
  });
  writeSse(res, { task_id: taskId, kind: "full", mutations: [], state: wireState(taskId) });
  // seeded 的运行中 Run 没有执行者：首个订阅到来时模拟后端继续执行它。
  if (isActive(states.get(taskId).activity?.status) && states.get(taskId).activity.status === "running" && !executors.has(taskId)) {
    continueRun(taskId);
  }
}

/** 编辑排队消息：原地改正文，消息已并入时 409 MESSAGE_CONSUMED。 */
function handleQueuedEdit(res, body, taskId, messageId) {
  const state = states.get(taskId);
  const queued = state?.queued_messages?.find((message) => message.id === messageId);
  if (!queued) {
    jsonResponse(res, 409, { detail: { error: { code: "MESSAGE_CONSUMED", message: "消息已被处理", retryable: false } } });
    return;
  }
  const next = structuredClone(state);
  next.queued_messages.find((message) => message.id === messageId).parts = body.message.parts;
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
      inputHolder: null,
      commandRunning: false,
      handoff: null,
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

function setTerminalHolder(session, holder) {
  if (session.entry.inputHolder === holder) return;
  session.entry.inputHolder = holder;
  if (states.has(session.taskId)) commit(session.taskId, states.get(session.taskId));
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
      // 最小租约模拟：人工输入即持有，短暂空闲后释放（真实后端为 3 秒）；与后端一致只经 snapshot 投影。
      clearTimeout(session.leaseTimer);
      setTerminalHolder(session, "user");
      session.leaseTimer = setTimeout(() => setTerminalHolder(session, null), 300);
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
  res.setHeader("Access-Control-Allow-Origin", "http://127.0.0.1:4173");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Trace-Id");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS");
  res.setHeader("Access-Control-Expose-Headers", "X-Cosir-Task-Id");

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    res.end();
    return;
  }

  const url = new URL(req.url ?? "/", `http://${HOST}:${PORT}`);

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
    jsonResponse(res, 200, [...tasks.values()].filter(task => task.task_type !== "delegate_task").map(taskProjection));
    return;
  }
  if (req.method === "POST" && url.pathname === `/workspaces/${WORKSPACE_ID}/tasks`) {
    const body = await readJson(req);
    const text = typeof body.text === "string" ? body.text : "";
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: text.slice(0, 40),
      execution_status: null,
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, emptyState());
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
    jsonResponse(res, 200, { task_id: taskId, deleted: true });
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
      "Access-Control-Allow-Origin": "http://127.0.0.1:4173",
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
      execution_status: null,
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(taskId, emptyState());
    jsonResponse(res, 200, { task_id: taskId });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-state") {
    // 直接落定某个 Task 的 canonical state：state 读取与 attach 首帧共用这一份事实。
    const body = await readJson(req);
    executors.delete(Number(body.taskId));
    commit(Number(body.taskId), body.state);
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
      execution_status: "running",
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, stateWithExchange(emptyState(), text, 899, "", "running"));
    executors.delete(TASK_ID);
    jsonResponse(res, 200, { task_id: TASK_ID });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-tool-trace") {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "工具追踪视觉回归",
      execution_status: null,
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, toolTraceState());
    jsonResponse(res, 200, { task_id: TASK_ID });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-web-search") {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "网页搜索标题展示回归",
      execution_status: null,
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, webSearchState());
    jsonResponse(res, 200, { task_id: TASK_ID });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-mermaid") {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "Mermaid 全屏回归",
      execution_status: null,
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, mermaidState());
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
  if (req.method === "POST" && url.pathname === "/__test__/terminal/sync") {
    const body = await readJson(req);
    if (states.has(body.taskId)) commit(body.taskId, states.get(body.taskId));
    jsonResponse(res, 200, {});
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/fail-next-attach") {
    failNextAttach = true;
    jsonResponse(res, 200, { enabled: true });
    return;
  }
  if (req.method === "POST" && url.pathname === "/__test__/seed-delegation") {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: "委派主任务",
      execution_status: "completed",
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(TASK_ID, delegationState());
    for (const id of [501, 502]) tasks.set(id, {
      task_id: id, workspace_id: WORKSPACE_ID, title: `子任务${id}`, task_type: "delegate_task",
      created_at: "2026-01-01T00:00:00.000Z", updated_at: "2026-01-01T00:00:00.000Z",
    });
    states.set(501, childDelegationState());
    states.set(502, secondChildDelegationState());
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
  const deferredRoute = url.pathname.match(/^\/tasks\/(\d+)\/tool-calls\/([^/]+)\/(cancel|background)$/);
  if (req.method === "POST" && deferredRoute) {
    // 与后端一致：登记过的调用按 tool_call_id 撤销或转后台；后台占位随撤销推到 cancelled。
    const taskId = Number(deferredRoute[1]);
    const toolCallId = decodeURIComponent(deferredRoute[2]);
    const entries = backgroundTasks.get(taskId) ?? [];
    const waits = foregroundWaits.get(taskId) ?? [];
    const detach = deferredRoute[3] === "background";
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
        parts: message.parts.map((part) => part.type === "tool-call" && part.toolCallId === toolCallId && part.deferred
          ? { ...part, status: "cancelled", error: "已取消" } : part),
      }));
      states.set(taskId, { ...state, messages });
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
  if (req.method === "PATCH" && queuedRoute) {
    handleQueuedEdit(res, await readJson(req), Number(queuedRoute[1]), decodeURIComponent(queuedRoute[2]));
    return;
  }
  if (req.method === "GET" && /^\/tasks\/\d+$/.test(url.pathname)) {
    const taskId = Number(url.pathname.split("/")[2]);
    const task = tasks.get(taskId);
    if (!task) {
      jsonResponse(res, 404, { detail: "task not found" });
      return;
    }
    jsonResponse(res, 200, { ...taskProjection(task), task_type: task.task_type ?? "user" });
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
  const taskRoute = url.pathname.match(/^\/tasks\/(\d+)\/(messages|resume|messages\/([^/]+)\/rerun)$/);
  if (req.method === "POST" && taskRoute) {
    const routeTaskId = Number(taskRoute[1]);
    try {
      if (taskRoute[2] === "resume") handleBusinessResume(res, routeTaskId);
      else handleMessage(res, await readJson(req), routeTaskId, taskRoute[3] ? decodeURIComponent(taskRoute[3]) : null);
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
