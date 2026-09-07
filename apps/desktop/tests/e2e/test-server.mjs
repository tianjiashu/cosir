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
  lastStreamBody = "";
  nextRunId = 1;
  nextResumeRunId = 900;
  telemetry.clientCancelCount = 0;
  telemetry.completedStreamCount = 0;
  telemetry.requestBodies = [];
}

const emptyUsage = () => ({
  input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  cache_hit_tokens: 0,
  cache_miss_tokens: 0,
  reasoning_tokens: 0,
});

const emptyState = () => ({
  messages: [],
  run: { runId: null, status: "idle" },
  approvals: {},
  context_usage: 0,
  usage: emptyUsage(),
  error: null,
});

const textMessage = (id, runId, role, text, status) => ({
  id,
  runId,
  role,
  status,
  endReason: status === "completed" ? "stop" : null,
  parts: role === "assistant"
    ? [
        { type: "reasoning", text: status === "completed" ? "推理完成" : "正在推理", status: status === "completed" ? "completed" : "running" },
        { type: "text", text, status: status === "completed" ? "completed" : "running" },
      ]
    : [{ type: "text", text, status: status === "completed" ? "completed" : "running" }],
});

function stateWithExchange(previous, text, runId, assistantText, status) {
  const next = structuredClone(previous);
  next.messages.push(
    textMessage(`user-${runId}`, runId, "user", text, "completed"),
    textMessage(`assistant-${runId}`, runId, "assistant", assistantText, status),
  );
  next.run = { runId, status: status === "completed" ? "completed" : "running" };
  return next;
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

function assistantFrame(operations) {
  return { type: "update-state", operations };
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
  res.on("close", () => {
    closed = true;
    if (generation === testGeneration && !res.writableEnded) telemetry.clientCancelCount += 1;
  });

  writeSse(res, assistantFrame([{ type: "set", path: [], value: initialState }]));
  await wait(120);
  if (generation !== testGeneration) {
    res.end();
    return;
  }
  if (cancelledRuns.has(runId)) {
    const cancelledState = structuredClone(initialState);
    cancelledState.messages[assistantIndex].status = "cancelled";
    cancelledState.messages[assistantIndex].endReason = "user_cancelled";
    cancelledState.run = { runId, status: "cancelled" };
    writeSse(res, assistantFrame([
      { type: "set", path: ["messages", assistantIndex], value: cancelledState.messages[assistantIndex] },
      { type: "set", path: ["run"], value: cancelledState.run },
    ]));
    writeSse(res, { type: "message-finish", finishReason: "cancelled" });
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
    if (cancelledRuns.has(runId)) {
      const cancelledState = structuredClone(initialState);
      cancelledState.messages[assistantIndex].status = "cancelled";
      cancelledState.messages[assistantIndex].endReason = "user_cancelled";
      cancelledState.run = { runId, status: "cancelled" };
      writeSse(res, assistantFrame([
        { type: "set", path: ["messages", assistantIndex], value: cancelledState.messages[assistantIndex] },
        { type: "set", path: ["run"], value: cancelledState.run },
      ]));
      writeSse(res, { type: "message-finish", finishReason: "cancelled" });
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
          path: ["messages", assistantIndex, "parts", 0, "text"],
          value: "先分析一下。",
        },
        {
          type: "append-text",
          path: ["messages", assistantIndex, "parts", 1, "text"],
          value: chunk,
        },
      ]),
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
      { type: "set", path: ["messages", assistantIndex], value: finalState.messages[assistantIndex] },
      { type: "set", path: ["run"], value: finalState.run },
    ]),
  );
  writeSse(res, { type: "message-finish", finishReason: "stop" });
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
    const sourceIndex = previous.messages.findIndex((message) => message.id === sourceId);
    if (sourceIndex >= 0) {
      branchBase = structuredClone(previous);
      branchBase.messages = branchBase.messages.slice(0, sourceIndex);
    }
  }
  const initialState = stateWithExchange(branchBase, text, runId, "", "running");
  const assistantIndex = initialState.messages.length - 1;
  let finalText;
  let chunks;

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
  states.set(taskId, finalState);

  res.setHeader("X-Cosir-Task-Id", String(taskId));
  res.setHeader("X-Cosir-Thread-Id", `task-${taskId}`);
  await streamState(res, initialState, finalState, assistantIndex, chunks, runId, testGeneration);
}

async function handleResume(req, res, body) {
  const taskId = Number.isInteger(body.taskId) ? body.taskId : TASK_ID;
  const previous = states.get(taskId);
  const lastMessage = previous?.messages.at(-1);
  const resumableCancelled = previous?.run.status === "cancelled"
    && lastMessage?.role === "assistant"
    && lastMessage.runId === previous.run.runId
    && lastMessage.endReason === "user_cancelled";
  if (!previous || (!resumableCancelled && previous.run.status !== "pending" && previous.run.status !== "running")) {
    res.writeHead(204);
    res.end();
    return;
  }

  const runId = previous.run.runId ?? nextResumeRunId++;
  cancelledRuns.delete(runId);
  const initialState = structuredClone(previous);
  const assistantIndex = initialState.messages.length - 1;
  const finalState = structuredClone(previous);
  finalState.messages[assistantIndex] = textMessage(
    finalState.messages[assistantIndex].id,
    runId,
    "assistant",
    "resumed response",
    "completed",
  );
  finalState.run = { runId, status: "completed" };
  states.set(taskId, finalState);
  res.setHeader("X-Cosir-Task-Id", String(taskId));
  res.setHeader("X-Cosir-Thread-Id", `task-${taskId}`);
  await streamState(res, initialState, finalState, assistantIndex, ["resumed", " response"], runId, testGeneration);
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
  if (req.method === "POST" && url.pathname.startsWith("/runs/") && url.pathname.endsWith("/cancel")) {
    const runId = Number(url.pathname.split("/")[2]);
    cancelledRuns.add(runId);
    for (const [taskId, state] of states) {
      if (state.run.runId !== runId) continue;
      const cancelledState = structuredClone(state);
      cancelledState.run = { runId, status: "cancelled" };
      const assistantMessage = cancelledState.messages.at(-1);
      if (assistantMessage?.role === "assistant") {
        assistantMessage.status = "cancelled";
        assistantMessage.endReason = "user_cancelled";
      }
      states.set(taskId, cancelledState);
    }
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
