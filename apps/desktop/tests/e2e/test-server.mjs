/* global structuredClone, setTimeout, URL, console, process */

import { createServer } from "node:http";

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
let lastStreamBody = "";
let nextRunId = 1;
let nextResumeRunId = 900;
let testGeneration = 0;
const cancelledRuns = new Set();
// taskId -> 下一次 attach 要推送的本轮输出；由命令回执登记，attach 消费一次。
const pendingStreams = new Map();
const dropCancelledStreams = new Set();
let dropNextCancelledStream = false;
let failNextAttach = false;
const telemetry = {
  clientCancelCount: 0,
  completedStreamCount: 0,
  requestBodies: [],
};

function resetTestState() {
  testGeneration += 1;
  tasks.clear();
  states.clear();
  cancelledRuns.clear();
  pendingStreams.clear();
  dropCancelledStreams.clear();
  dropNextCancelledStream = false;
  failNextAttach = false;
  lastStreamBody = "";
  nextRunId = 1;
  nextResumeRunId = 900;
  telemetry.clientCancelCount = 0;
  telemetry.completedStreamCount = 0;
  telemetry.requestBodies = [];
}

function taskProjection(task) {
  const state = states.get(task.task_id);
  const currentRunId = state?.current_run_id ?? null;
  return { ...task, current_run_id: currentRunId, execution_status: state?.runs.find(run => run.runId === currentRunId)?.status ?? null };
}

const emptyState = () => ({
  runs: [],
  current_run_id: null,
  approvals: {},

  error: null,
});

const textMessage = (id, role, text, status) => ({
  id,
  role,
  parts: role === "assistant"
    ? [
        { type: "reasoning", text: status === "completed" ? "推理完成" : "正在推理", status: status === "completed" ? "completed" : "running" },
        { type: "text", text, status: status === "completed" ? "completed" : "running" },
      ]
    : [{ type: "text", text, status: status === "completed" ? "completed" : "running" }],
});

function stateWithExchange(previous, text, runId, assistantText, status) {
  const next = structuredClone(previous);
  const terminal = status === "completed" || status === "failed" || status === "cancelled";
  next.runs.push({
    runId,
    status: terminal ? status : "running",
    endReason: status === "completed" ? "stop" : status === "failed" ? "tool_error_limit_reached" : status === "cancelled" ? "user_cancelled" : null,
    contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
    messages: [
      textMessage(`user-${runId}`, "user", text, "completed"),
      textMessage(`assistant-${runId}`, "assistant", assistantText, status),
    ],
    usage: null,
    error: null,
  });
  next.current_run_id = runId;
  return next;
}

function toolLifecycleState(previous, text, runId, status, toolStatus) {
  const next = stateWithExchange(previous, text, runId, "", status);
  const assistant = next.runs.at(-1).messages.at(-1);
  assistant.parts = [
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
  return next;
}

function applyUsageFixture(state, runId, secondRun) {
  const input = secondRun ? 2_000 : 1_000;
  const output = secondRun ? 500 : 500;
  const total = input + output;
  const run = state.runs.find((candidate) => candidate.runId === runId);
  run.usage = {
    input_tokens: input,
    output_tokens: output,
    total_tokens: total,
    cache_hit_tokens: secondRun ? 200 : 100,
    cache_miss_tokens: secondRun ? 50 : null,
    reasoning_tokens: secondRun ? 40 : 20,
  };
  run.contextWindowTotal = 100_000;
}

function toolTraceState() {
  return {
    runs: [{
      runId: 77,
      status: "completed",
      endReason: "stop",
    contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
      messages: [
      {
        id: "user-tool-trace",
        role: "user",
        parts: [{ type: "text", text: "检查项目", status: "completed" }],
      },
      {
        id: "assistant-tool-trace",
        role: "assistant",
        parts: [
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
        ],
      },
      ],
      usage: null,
      error: null,
    }],
    current_run_id: 77,
    approvals: {},

    error: null,
  };
}

function webSearchState() {
  return {
    runs: [{
      runId: 78,
      status: "completed",
      endReason: "stop",
    contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
      messages: [
      {
        id: "user-web-search",
        role: "user",
        parts: [{ type: "text", text: "搜索 assistant-ui", status: "completed" }],
      },
      {
        id: "assistant-web-search",
        role: "assistant",
        parts: [
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
        ],
      },
      ],
      usage: null,
      error: null,
    }],
    current_run_id: 78,
    approvals: {},

    error: null,
  };
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
  return {
    runs: [{
      runId: 500,
      status: "completed",
      endReason: "stop",
    contextWindowTotal: null, launch: { modelConfigId: null, reasoningEffort: null, banTools: [] },
      messages: [
        {
          id: "user-delegation",
          role: "user",
          parts: [{ type: "text", text: "请委派一个审查任务", status: "completed" }],
        },
        {
          id: "assistant-delegation",
          role: "assistant",
          parts: [
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
          ],
        },
      ],
      usage: null,
      error: null,
    }],
    current_run_id: 500,
    approvals: {},

    error: null,
  };
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
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "X-Cosir-Task-Id",
    "Content-Type": "application/json; charset=utf-8",
  });
  res.end(JSON.stringify(value));
}

function assistantFrame(operations, targetRunId, targetRunStatus = null, taskId = TASK_ID) {
  const rootOperation = operations.find((operation) => operation.path.length === 0);
  if (rootOperation) {
    const state = rootOperation.value;
    const resolvedRunId = targetRunId ?? state.current_run_id;
    const resolvedStatus = targetRunStatus
      ?? state.runs.find((run) => run.runId === resolvedRunId)?.status
      ?? null;
    return {
      task_id: taskId,
      kind: "full",
      mutations: [],
      state,
      target_run_id: resolvedRunId,
      target_run_status: resolvedStatus,
    };
  }
  return {
    task_id: taskId,
    kind: "mutation",
    mutations: operations.map(({ type, ...operation }) => ({ kind: type, ...operation })),
    source_run_id: targetRunId,
    target_run_id: targetRunId,
    target_run_status: targetRunStatus,
  };
}

function writeSse(res, chunk) {
  const frame = `data: ${JSON.stringify(chunk)}\n\n`;
  lastStreamBody += frame;
  res.write(frame);
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function streamState(res, initialState, finalState, assistantIndex, chunks, runId, generation, taskId) {
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  lastStreamBody = "";
  res.writeHead(200, {
    "Access-Control-Allow-Origin": "http://127.0.0.1:4173",
    "Access-Control-Allow-Headers": "Content-Type, X-Trace-Id",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "X-Cosir-Task-Id",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "Content-Type": "text/event-stream; charset=utf-8",
  });

  let closed = false;
  const runIndex = initialState.runs.findIndex((run) => run.runId === runId);
  res.on("close", () => {
    closed = true;
    if (generation === testGeneration && !res.writableEnded) telemetry.clientCancelCount += 1;
  });

  writeSse(res, assistantFrame([{ type: "set", path: [], value: initialState }], runId, "running", taskId));
  await wait(120);
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  if (dropCancelledStreams.delete(runId)) {
    res.destroy();
    return;
  }
  if (cancelledRuns.has(runId)) {
    const cancelledState = structuredClone(initialState);
    cancelledState.runs[runIndex].status = "cancelled";
    cancelledState.runs[runIndex].endReason = "user_cancelled";
    writeSse(res, assistantFrame([
      { type: "set", path: ["runs", runIndex], value: cancelledState.runs[runIndex] },
    ], runId, "cancelled", taskId));
    lastStreamBody += "data: [DONE]\n\n";
    res.write("data: [DONE]\n\n");
    res.end();
    telemetry.completedStreamCount += 1;
    return;
  }
  for (const chunk of chunks) {
    if (closed || generation !== testGeneration) {
      res.end();
      return;
    }
    if (dropCancelledStreams.delete(runId)) {
      res.destroy();
      return;
    }
    if (cancelledRuns.has(runId)) {
      const cancelledState = structuredClone(initialState);
      cancelledState.runs[runIndex].status = "cancelled";
      cancelledState.runs[runIndex].endReason = "user_cancelled";
      writeSse(res, assistantFrame([
        { type: "set", path: ["runs", runIndex], value: cancelledState.runs[runIndex] },
      ], runId, "cancelled", taskId));
      lastStreamBody += "data: [DONE]\n\n";
      res.write("data: [DONE]\n\n");
      res.end();
      telemetry.completedStreamCount += 1;
      return;
    }
    writeSse(
      res,
      assistantFrame([
        {
          type: "append-text",
          path: ["runs", runIndex, "messages", assistantIndex, "parts", 0, "text"],
          value: "先分析一下。",
        },
        {
          type: "append-text",
          path: ["runs", runIndex, "messages", assistantIndex, "parts", 1, "text"],
          value: chunk,
        },
      ], runId, "running", taskId),
    );
    await wait(420);
  }

  if (closed || generation !== testGeneration) {
    res.end();
    return;
  }
  writeSse(
    res,
    assistantFrame([
      { type: "set", path: ["runs", runIndex], value: finalState.runs[runIndex] },
    ], runId, finalState.runs[runIndex].status, taskId),
  );
  lastStreamBody += "data: [DONE]\n\n";
  res.write("data: [DONE]\n\n");
  res.end();
  telemetry.completedStreamCount += 1;
}

async function streamToolLifecycle(res, initialState, finalState, runId, generation, taskId) {
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  lastStreamBody = "";
  res.writeHead(200, {
    "Access-Control-Allow-Origin": "http://127.0.0.1:4173",
    "Access-Control-Allow-Headers": "Content-Type, X-Trace-Id",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "X-Cosir-Task-Id",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "Content-Type": "text/event-stream; charset=utf-8",
  });
  writeSse(res, assistantFrame([{ type: "set", path: [], value: initialState }], runId, "running", taskId));
  await wait(250);
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  writeSse(res, assistantFrame([{ type: "set", path: ["runs", 0, "messages", 1, "parts", 0, "status"], value: "running" }], runId, "running", taskId));
  await wait(250);
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  writeSse(res, assistantFrame([{ type: "set", path: ["runs", 0], value: finalState.runs[0] }], runId, finalState.runs[0].status, taskId));
  lastStreamBody += "data: [DONE]\n\n";
  res.write("data: [DONE]\n\n");
  res.end();
  telemetry.completedStreamCount += 1;
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
 * 模拟 POST /tasks/{id}/messages 与 /messages/{messageId}/edit：只返回 JSON 回执，把本轮输出登记为
 * 待推流，由随后的 attach 订阅推送；与生产「命令回执 + attach 订阅」的两段语义一致。
 */
function handleMessage(res, body, taskId, sourceId) {
  telemetry.requestBodies.push({ ...body, taskId, ...(sourceId ? { sourceId } : {}) });
  const text = commandText(body);
  if (!text.trim()) {
    jsonResponse(res, 400, { error: { code: "INVALID_MESSAGE", message: "message is required", retryable: false } });
    return;
  }

  const runId = nextRunId++;
  const previous = states.get(taskId) ?? emptyState();
  if (typeof sourceId === "string" && text === "edit-failure") {
    jsonResponse(res, 409, {
      error: { code: "EDIT_REJECTED", message: "编辑重跑被测试后端拒绝", retryable: true },
    });
    return;
  }
  let branchBase = previous;
  if (typeof sourceId === "string") {
    const sourceRunIndex = previous.runs.findIndex((run) => run.messages.some((message) => message.id === sourceId));
    const sourceRun = sourceRunIndex < 0 ? null : previous.runs[sourceRunIndex];
    const sourceIndex = sourceRun?.messages.findIndex((message) => message.id === sourceId) ?? -1;
    if (sourceRun && sourceIndex >= 0) {
      branchBase = structuredClone(previous);
      branchBase.runs = branchBase.runs.slice(0, sourceRunIndex + 1);
      branchBase.runs[sourceRunIndex].messages = branchBase.runs[sourceRunIndex].messages.slice(0, sourceIndex);
    }
  }
  const initialState = stateWithExchange(branchBase, text, runId, "", "running");
  const assistantIndex = initialState.runs.at(-1).messages.length - 1;
  let finalText;
  let chunks;

  if (text.startsWith("tool-lifecycle")) {
    const toolInitialState = toolLifecycleState(branchBase, text, runId, "running", "pending");
    const finalStatus = text.startsWith("tool-lifecycle-failed") ? "failed"
      : text.startsWith("tool-lifecycle-cancelled") ? "cancelled"
        : "completed";
    const finalToolStatus = finalStatus === "completed" ? "completed" : finalStatus;
    const toolFinalState = toolLifecycleState(branchBase, text, runId, finalStatus, finalToolStatus);
    states.set(taskId, toolFinalState);
    pendingStreams.set(taskId, (attachRes) => streamToolLifecycle(attachRes, toolInitialState, toolFinalState, runId, testGeneration, taskId));
    jsonResponse(res, 200, { state: toolInitialState, queued: false });
    return;
  }

  finalText = "streaming response";
  chunks = ["stream", "ing response"];

  const finalState = stateWithExchange(branchBase, text, runId, finalText, "completed");
  if (text.startsWith("usage-regression")) {
    applyUsageFixture(initialState, runId, text.includes("second"));
    applyUsageFixture(finalState, runId, text.includes("second"));
  }
  states.set(taskId, finalState);
  pendingStreams.set(taskId, (attachRes) => streamState(attachRes, initialState, finalState, assistantIndex, chunks, runId, testGeneration, taskId));
  jsonResponse(res, 200, { state: initialState, queued: false });
}

/**
 * 计算续跑的初始与最终快照。``business`` 为真时模拟 POST /tasks/{id}/resume（只接受用户取消的
 * Run），否则模拟 attach 订阅一个仍在执行的 seeded Run。不可续跑时返回 null。
 */
function resumePlan(taskId, { business }) {
  const previous = states.get(taskId);
  const previousRun = previous?.runs.find((run) => run.runId === previous.current_run_id);
  const lastMessage = previousRun?.messages.at(-1);
  const resumableCancelled = previousRun?.status === "cancelled"
    && lastMessage?.role === "assistant"
    && previousRun.endReason === "user_cancelled";
  const running = previousRun?.status === "pending" || previousRun?.status === "running";
  if (!previous || (business ? !resumableCancelled : !running && !resumableCancelled)) return null;

  const runId = previous.current_run_id ?? nextResumeRunId++;
  cancelledRuns.delete(runId);
  const initialState = structuredClone(previous);
  const runIndex = initialState.runs.findIndex((run) => run.runId === runId);
  // 业务 resume 在推流前已把 Run 迁回 running；初始快照须与 target_run_status 一致。
  initialState.runs[runIndex].status = "running";
  initialState.runs[runIndex].endReason = null;
  const assistantIndex = initialState.runs[runIndex].messages.length - 1;
  const finalState = structuredClone(previous);
  finalState.runs[runIndex].messages[assistantIndex] = textMessage(
    finalState.runs[runIndex].messages[assistantIndex].id,
    "assistant",
    "resumed response",
    "completed",
  );
  finalState.runs[runIndex].status = "completed";
  finalState.runs[runIndex].endReason = "stop";
  states.set(taskId, finalState);
  return (attachRes) => streamState(attachRes, initialState, finalState, assistantIndex, ["resumed", " response"], runId, testGeneration, taskId);
}

function handleBusinessResume(res, taskId) {
  const stream = resumePlan(taskId, { business: true });
  if (!stream) {
    jsonResponse(res, 409, { error: { code: "RUN_NOT_RESUMABLE", message: "当前运行不可恢复", retryable: true } });
    return;
  }
  // 续跑前状态已迁回 running：回执快照与随后 attach 的首帧一致。
  const state = structuredClone(states.get(taskId));
  const run = state.runs.find((candidate) => candidate.runId === state.current_run_id);
  run.status = "running";
  run.endReason = null;
  pendingStreams.set(taskId, stream);
  jsonResponse(res, 200, { state, queued: false });
}

async function handleAttach(res, taskId) {
  res.setHeader("X-Cosir-Task-Id", String(taskId));
  const pending = pendingStreams.get(taskId);
  if (pending) {
    pendingStreams.delete(taskId);
    await pending(res);
    return;
  }
  const previous = states.get(taskId);
  const previousRun = previous?.runs.find((run) => run.runId === previous.current_run_id);
  const lastMessage = previousRun?.messages.at(-1);
  // A business resume may finish its executor before the UI's attach request
  // arrives. Replay the terminal canonical snapshot so the UI does not miss
  // the result merely because the two local HTTP streams are independent.
  if (previousRun?.status === "completed" && lastMessage?.role === "assistant") {
    const assistantIndex = previousRun.messages.length - 1;
    await streamState(res, previous, previous, assistantIndex, [], previousRun.runId, testGeneration, taskId);
    return;
  }
  const stream = resumePlan(taskId, { business: false });
  if (!stream) {
    jsonResponse(res, 409, { error: { code: "NO_RUN", message: "当前对话没有可订阅的运行", retryable: false } });
    return;
  }
  await stream(res);
}

const server = createServer(async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "http://127.0.0.1:4173");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Trace-Id");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
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
  if (req.method === "POST" && /^\/tasks\/\d+\/cancel$/.test(url.pathname)) {
    // 与后端一致按 task 取消：命中该 Task 的当前 Run。
    const cancelTaskId = Number(url.pathname.split("/")[2]);
    const runId = states.get(cancelTaskId)?.current_run_id ?? null;
    if (runId === null) {
      jsonResponse(res, 409, { detail: "task has no cancellable execution" });
      return;
    }
    cancelledRuns.add(runId);
    if (dropNextCancelledStream) {
      dropCancelledStreams.add(runId);
      dropNextCancelledStream = false;
    }
    for (const [taskId, state] of states) {
      const runIndex = state.runs.findIndex((run) => run.runId === runId);
      if (runIndex < 0) continue;
      const cancelledState = structuredClone(state);
      cancelledState.runs[runIndex].status = "cancelled";
      cancelledState.runs[runIndex].endReason = "user_cancelled";
      states.set(taskId, cancelledState);
    }
    // Give an already-connected Assistant Transport stream a turn to publish
    // the terminal cancelled snapshot before the client receives the ACK and
    // aborts its local request. This mirrors the production projector push.
    await wait(160);
    jsonResponse(res, 200, { task_id: cancelTaskId, cancelled: true });
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
    jsonResponse(res, 200, states.get(taskId));
    return;
  }
  const taskRoute = url.pathname.match(/^\/tasks\/(\d+)\/(messages|resume|messages\/([^/]+)\/edit)$/);
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
    try {
      if (failNextAttach) {
        failNextAttach = false;
        jsonResponse(res, 503, { error: { code: "ATTACH_TEMPORARY_FAILURE", message: "attach temporarily unavailable", retryable: true } });
        return;
      }
      // attach 只订阅：命令回执登记的待推流优先，否则订阅 seeded 的运行中 Run。
      await handleAttach(res, Number(url.pathname.split("/")[2]));
    } catch {
      if (!res.headersSent) jsonResponse(res, 400, { error: { code: "INVALID_REQUEST", message: "invalid request", retryable: false } });
      else res.destroy();
    }
    return;
  }
  jsonResponse(res, 404, { detail: "not found" });
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
