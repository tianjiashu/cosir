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

const emptyState = () => ({
  runs: [],
  current_run_id: null,
  approvals: {},
  context_usage_ratio: null,
  context_usage_used: null,
  context_window_total: null,
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
      data: toolStatus === "completed"
        ? { kind: "content-search-results", matches: [{ path: "lifecycle.test.ts", line: 1, content: "lifecycle", is_match: true }] }
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
  const contextUsed = secondRun ? 80_000 : 70_000;
  const run = state.runs.find((candidate) => candidate.runId === runId);
  run.usage = {
    input_tokens: input,
    output_tokens: output,
    total_tokens: total,
    cache_hit_tokens: secondRun ? 200 : 100,
    cache_miss_tokens: secondRun ? 50 : null,
    reasoning_tokens: secondRun ? 40 : 20,
  };
  state.context_usage_ratio = contextUsed / 100_000;
  state.context_usage_used = contextUsed;
  state.context_window_total = 100_000;
}

function toolTraceState() {
  return {
    runs: [{
      runId: 77,
      status: "completed",
      endReason: "stop",
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
            data: null,
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
            data: null,
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
    context_usage_ratio: null,
    context_usage_used: null,
    context_window_total: null,
    error: null,
  };
}

function webSearchState() {
  return {
    runs: [{
      runId: 78,
      status: "completed",
      endReason: "stop",
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
            data: {
              entries: [{
                name: "Assistant UI 官方文档",
                type: "link",
                path: "https://assistant-ui.com/docs",
              }],
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
    context_usage_ratio: null,
    context_usage_used: null,
    context_window_total: null,
    error: null,
  };
}

function delegationState() {
  return {
    runs: [{
      runId: 500,
      status: "completed",
      endReason: "stop",
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
    context_usage_ratio: null,
    context_usage_used: null,
    context_window_total: null,
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
    "Access-Control-Expose-Headers": "X-Cosir-Task-Id, X-Cosir-Thread-Id",
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

async function streamState(res, initialState, finalState, assistantIndex, chunks, runId, generation) {
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  lastStreamBody = "";
  res.writeHead(200, {
    "Access-Control-Allow-Origin": "http://127.0.0.1:4173",
    "Access-Control-Allow-Headers": "Content-Type, X-Trace-Id",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "X-Cosir-Task-Id, X-Cosir-Thread-Id",
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

  writeSse(res, assistantFrame([{ type: "set", path: [], value: initialState }], runId, "running"));
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
    ], runId, "cancelled"));
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
      ], runId, "cancelled"));
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
      ], runId, "running"),
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
    ], runId, finalState.runs[runIndex].status),
  );
  lastStreamBody += "data: [DONE]\n\n";
  res.write("data: [DONE]\n\n");
  res.end();
  telemetry.completedStreamCount += 1;
}

async function streamToolLifecycle(res, initialState, finalState, runId, generation) {
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  lastStreamBody = "";
  res.writeHead(200, {
    "Access-Control-Allow-Origin": "http://127.0.0.1:4173",
    "Access-Control-Allow-Headers": "Content-Type, X-Trace-Id",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Expose-Headers": "X-Cosir-Task-Id, X-Cosir-Thread-Id",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
    "Content-Type": "text/event-stream; charset=utf-8",
  });
  writeSse(res, assistantFrame([{ type: "set", path: [], value: initialState }], runId, "running"));
  await wait(250);
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  writeSse(res, assistantFrame([{ type: "set", path: ["runs", 0, "messages", 1, "parts", 0, "status"], value: "running" }], runId, "running"));
  await wait(250);
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  writeSse(res, assistantFrame([{ type: "set", path: ["runs", 0], value: finalState.runs[0] }], runId, finalState.runs[0].status));
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
  return body?.commands?.[0]?.message?.parts
    ?.filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n") ?? "";
}

async function handleAssistant(req, res, body) {
  telemetry.requestBodies.push(body);
  const text = commandText(body);
  if (!text.trim()) {
    jsonResponse(res, 400, { error: { code: "INVALID_MESSAGE", message: "message is required", retryable: false } });
    return;
  }

  const taskId = Number.isInteger(body.taskId) ? body.taskId : TASK_ID;
  const runId = nextRunId++;
  const previous = states.get(taskId) ?? emptyState();
  const sourceId = body?.commands?.find((command) => command?.type === "add-message")?.sourceId;
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
    res.setHeader("X-Cosir-Task-Id", String(taskId));
    res.setHeader("X-Cosir-Thread-Id", `task-${taskId}`);
    await streamToolLifecycle(res, toolInitialState, toolFinalState, runId, testGeneration);
    return;
  }

  if (body.taskId === undefined) {
    tasks.set(TASK_ID, {
      task_id: TASK_ID,
      workspace_id: WORKSPACE_ID,
      title: text.slice(0, 40),
      execution_status: "completed",
      created_at: "2026-01-01T00:00:00.000Z",
      updated_at: "2026-01-01T00:00:00.000Z",
    });
    finalText = "world";
    chunks = ["world"];
  } else {
    finalText = "streaming response";
    chunks = ["stream", "ing response"];
  }

  const finalState = stateWithExchange(branchBase, text, runId, finalText, "completed");
  if (text.startsWith("usage-regression")) {
    applyUsageFixture(initialState, runId, text.includes("second"));
    applyUsageFixture(finalState, runId, text.includes("second"));
  }
  states.set(taskId, finalState);

  res.setHeader("X-Cosir-Task-Id", String(taskId));
  res.setHeader("X-Cosir-Thread-Id", `task-${taskId}`);
  await streamState(res, initialState, finalState, assistantIndex, chunks, runId, testGeneration);
}

async function handleResume(req, res, body) {
  const taskId = Number.isInteger(body.taskId) ? body.taskId : TASK_ID;
  const previous = states.get(taskId);
  const previousRun = previous?.runs.find((run) => run.runId === previous.current_run_id);
  const lastMessage = previousRun?.messages.at(-1);
  const resumableCancelled = previousRun?.status === "cancelled"
    && lastMessage?.role === "assistant"
    && previousRun.endReason === "user_cancelled";
  if (!previous || (!resumableCancelled && previousRun?.status !== "pending" && previousRun?.status !== "running")) {
    res.writeHead(204);
    res.end();
    return;
  }

  const runId = previous.current_run_id ?? nextResumeRunId++;
  cancelledRuns.delete(runId);
  const initialState = structuredClone(previous);
  const runIndex = initialState.runs.findIndex((run) => run.runId === runId);
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
  res.setHeader("X-Cosir-Task-Id", String(taskId));
  res.setHeader("X-Cosir-Thread-Id", `task-${taskId}`);
  await streamState(res, initialState, finalState, assistantIndex, ["resumed", " response"], runId, testGeneration);
}

async function handleAttach(req, res, body) {
  const taskId = Number.isInteger(body.taskId) ? body.taskId : TASK_ID;
  const previous = states.get(taskId);
  const previousRun = previous?.runs.find((run) => run.runId === previous.current_run_id);
  const lastMessage = previousRun?.messages.at(-1);
  // A business resume may finish its executor before the UI's attach request
  // arrives. Replay the terminal canonical snapshot so the UI does not miss
  // the result merely because the two local HTTP streams are independent.
  if (previousRun?.status === "completed" && lastMessage?.role === "assistant") {
    const assistantIndex = previousRun.messages.length - 1;
    res.setHeader("X-Cosir-Task-Id", String(taskId));
    res.setHeader("X-Cosir-Thread-Id", `task-${taskId}`);
    await streamState(res, previous, previous, assistantIndex, [], previousRun.runId, testGeneration);
    return;
  }
  await handleResume(req, res, body);
}

const server = createServer(async (req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "http://127.0.0.1:4173");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-Trace-Id");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  res.setHeader("Access-Control-Expose-Headers", "X-Cosir-Task-Id, X-Cosir-Thread-Id");

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
    jsonResponse(res, 200, [...tasks.values()]);
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
  if (req.method === "GET" && url.pathname === "/models") {
    jsonResponse(res, 200, [{
      provider_id: 2,
      provider_name: "demo",
      models: [{
        model_name: "demo-model",
        supports_thinking: false,
        supports_image: false,
        supports_video: false,
        supports_reasoning_effort: false,
      }],
    }]);
    return;
  }
  if (req.method === "GET" && url.pathname === "/tools/groups") {
    jsonResponse(res, 200, {
      groups: [{ group: "文件", tools: [{ name: "read_file", description: "读取文件" }] }],
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
    states.set(501, childDelegationState());
    states.set(502, secondChildDelegationState());
    jsonResponse(res, 200, { task_id: TASK_ID, child_task_id: 501, second_child_task_id: 502 });
    return;
  }
  if (req.method === "POST" && url.pathname.startsWith("/runs/") && url.pathname.endsWith("/cancel")) {
    const runId = Number(url.pathname.split("/")[2]);
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
    jsonResponse(res, 200, { accepted: true });
    return;
  }
  if (req.method === "GET" && /^\/tasks\/\d+$/.test(url.pathname)) {
    const taskId = Number(url.pathname.split("/")[2]);
    const task = tasks.get(taskId);
    if (!task) {
      jsonResponse(res, 404, { detail: "task not found" });
      return;
    }
    jsonResponse(res, 200, { ...task, task_type: task.task_type ?? "user", fork_available: task.fork_available ?? true });
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
  if (req.method === "POST" && /^\/tasks\/\d+\/assistant\/attach$/.test(url.pathname)) {
    try {
      const body = await readJson(req);
      if (failNextAttach) {
        failNextAttach = false;
        jsonResponse(res, 503, { error: { code: "ATTACH_TEMPORARY_FAILURE", message: "attach temporarily unavailable", retryable: true } });
        return;
      }
      // The fixture models an already-running local executor. In production
      // this endpoint only attaches the UI stream; it must not be confused
      // with the user-triggered business resume on /assistant.
      await handleAttach(req, res, body);
    } catch {
      if (!res.headersSent) jsonResponse(res, 400, { error: { code: "INVALID_REQUEST", message: "invalid request", retryable: false } });
      else res.destroy();
    }
    return;
  }
  if (req.method === "POST" && url.pathname === "/assistant") {
    try {
      const body = await readJson(req);
      if (Array.isArray(body.commands) && body.commands.length === 0) {
        await handleResume(req, res, body);
      } else {
        await handleAssistant(req, res, body);
      }
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
