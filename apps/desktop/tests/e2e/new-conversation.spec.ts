import { expect, test } from "@playwright/test";

type FrontendLog = {
  event?: string;
  data?: Record<string, unknown>;
};

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
});

async function frontendLogs(page: import("@playwright/test").Page): Promise<FrontendLog[]> {
  return page.evaluate(() => {
    const value = (window as unknown as { __cosirFrontendLogs?: FrontendLog[] }).__cosirFrontendLogs;
    return value ?? [];
  });
}

test("新建对话请求、Assistant Transport 流和增量 UI 均正常工作", async ({ page, request }) => {
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-selection:workspace:7",
      JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }),
    );
  });
  const assistantRequests: Array<{ body: Record<string, unknown>; status: number }> = [];
  page.on("response", (response) => {
    if (!response.url().endsWith("/assistant")) return;
    const body = response.request().postDataJSON() as Record<string, unknown>;
    assistantRequests.push({ body, status: response.status() });
  });
  await page.route("http://127.0.0.1:8000/models", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([{
        provider_id: 1,
        provider_name: "demo",
        models: [{
          model_name: "demo-model",
          supports_thinking: false,
          supports_image: false,
          supports_video: false,
          supports_reasoning_effort: false,
        }],
      }]),
    });
  });

  await page.goto("/");
  await expect(page.getByText("demo").first()).toBeVisible();
  await page.getByRole("button", { name: "选择工作区" }).click();
  await page.getByRole("option", { name: /demo/ }).click();
  await expect(page.getByRole("combobox", { name: "选择模型" })).toContainText("demo-model");
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("cosir:model-selection:workspace:7"))).toBe(
    JSON.stringify({ providerId: 1, modelName: "demo-model", reasoningEffort: null }),
  );
  expect(await page.evaluate(() => window.localStorage.getItem("cosir:model-selection:default"))).toBeNull();
  await page.getByLabel("新对话内容").fill("你好");
  await expect(page.getByRole("button", { name: "开始对话" })).toBeEnabled();
  await page.getByRole("button", { name: "开始对话" }).click();

  await expect(page).toHaveURL(/\/tasks\/42$/);
  await expect.poll(() => assistantRequests.length).toBe(1);
  // WorkspaceShell refresh is a normal parent re-render and must not replace
  // the active Assistant Transport session or abort its response body.
  await page.getByRole("button", { name: "刷新工作区" }).click();
  expect(assistantRequests[0]?.status).toBe(200);
  expect(assistantRequests[0]?.body.workspaceId).toBe(7);
  expect(assistantRequests[0]?.body.providerId).toBe(1);
  expect(assistantRequests[0]?.body.taskId).toBe(42);
  expect(assistantRequests[0]?.body.commands).toHaveLength(1);
  expect((assistantRequests[0]?.body.commands as Array<{ message?: { parts?: Array<{ text?: string }> } }>)[0]?.message?.parts?.[0]?.text).toBe("你好");
  await expect(page.getByRole("main").getByText("你好", { exact: true })).toBeVisible();
  await expect(page.getByText("stream", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Reasoning", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "发送" })).toBeVisible();

  const messageInput = page.getByLabel("消息输入");
  const secondResponsePromise = page.waitForResponse((response) => {
    if (!response.url().endsWith("/assistant") || response.request().method() !== "POST") return false;
    const body = response.request().postDataJSON() as { taskId?: number };
    return body.taskId === 42;
  });
  await messageInput.fill("请继续");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("stream", { exact: true })).toBeVisible();
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
  await secondResponsePromise;
  const streamLogUrl = "http://127.0.0.1:8000/__test__/last-stream";
  await expect.poll(async () => (await request.get(streamLogUrl)).text()).toContain("data: [DONE]");
  const streamBody = await (await request.get(streamLogUrl)).text();
  await expect.poll(() => assistantRequests.length).toBe(2);
  expect(assistantRequests[1]?.status).toBe(200);
  expect(assistantRequests[1]?.body.taskId).toBe(42);
  expect(assistantRequests[1]?.body.state).toBeUndefined();
  expect(streamBody).toContain('"type":"update-state"');
  expect(streamBody).toContain('"type":"append-text"');
  expect(streamBody).toContain("streaming response");
  expect(streamBody).toContain("data: [DONE]");

  const logs = await frontendLogs(page);
  expect(logs.filter((entry) => entry.event === "assistant_runtime_unmounted")).toHaveLength(0);
  expect(logs.filter((entry) => entry.event === "assistant_transport_stream_cancelled")).toHaveLength(0);
  const finishes = logs.filter((entry) => entry.event === "assistant_transport_stream_finished");
  expect(logs.filter((entry) => entry.event === "assistant_transport_response_received")).toHaveLength(2);
  expect(finishes).toHaveLength(2);
  expect(finishes.at(-1)?.data?.pendingCommandCount).toBe(0);
  const telemetry = await (await request.get("http://127.0.0.1:8000/__test__/telemetry")).json() as {
    clientCancelCount: number;
  };
  expect(telemetry.clientCancelCount).toBe(0);
});

test("任务页面重挂载时自动恢复未结束的 run", async ({ page, request }) => {
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-selection:task:42",
      JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }),
    );
  });

  await request.post("http://127.0.0.1:8000/__test__/seed-running", {
    data: { text: "恢复测试" },
  });

  const resumeResponse = page.waitForResponse((response) => (
    response.url().endsWith("/tasks/42/assistant/attach") && response.request().method() === "POST"
  ));
  await page.goto("/tasks/42");
  await expect(page.getByRole("main").getByText("恢复测试", { exact: true })).toBeVisible();
  await expect(page.getByTestId("run-usage-display")).toHaveText("用量统计中…");
  await resumeResponse;
  await expect(page.getByText("resumed", { exact: true })).toBeVisible();
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  await expect.poll(async () => (await request.get("http://127.0.0.1:8000/__test__/last-stream")).text()).toContain("data: [DONE]");
});

test("不同 task 的 Assistant Transport 会话与消息互相隔离", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 100, title: "任务100" } });
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 101, title: "任务101" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-selection:task:100",
      JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }),
    );
  });

  await page.goto("/tasks/100");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByLabel("消息输入").fill("task-100-message");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("task-100-message", { exact: true })).toBeVisible();
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();

  await page.goto("/tasks/101");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await expect(page.getByText("task-100-message", { exact: true })).toHaveCount(0);
  await page.getByLabel("消息输入").fill("task-101-message");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("task-101-message", { exact: true })).toBeVisible();
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
  await expect(page.getByText("task-100-message", { exact: true })).toHaveCount(0);

  await page.goto("/tasks/100");
  await expect(page.getByText("task-100-message", { exact: true })).toBeVisible();
  await expect(page.getByText("task-101-message", { exact: true })).toHaveCount(0);
});

test("停止按钮通过后端取消当前 run，且不会复用后续命令", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 102, title: "任务102" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-selection:task:102",
      JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }),
    );
  });

  const assistantRequests: Array<{ body: Record<string, unknown>; status: number }> = [];
  const assistantStateRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/assistant/state")) assistantStateRequests.push(request.url());
  });
  page.on("response", (response) => {
    if (!response.url().endsWith("/assistant")) return;
    assistantRequests.push({
      body: response.request().postDataJSON() as Record<string, unknown>,
      status: response.status(),
    });
  });

  await page.goto("/tasks/102");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  const stateReadsBeforeCancel = assistantStateRequests.length;
  await page.getByLabel("消息输入").fill("cancel-me");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("stream", { exact: true })).toBeVisible();
  const cancelResponse = page.waitForResponse((response) => (
    response.url().includes("/runs/")
    && response.url().endsWith("/cancel")
    && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "停止" }).click();
  await expect((await cancelResponse).status()).toBe(200);
  await expect(page.getByRole("button", { name: "继续运行" })).toBeVisible();
  expect(assistantStateRequests).toHaveLength(stateReadsBeforeCancel);
  expect(assistantRequests).toHaveLength(1);

  const resumeResponse = page.waitForResponse((response) => (
    response.url().endsWith("/assistant") && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "继续运行" }).click();
  await expect((await resumeResponse).status()).toBe(200);
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  await expect.poll(() => assistantRequests.length).toBe(2);
  expect(assistantRequests[1]?.body.runId).toBe(1);
  expect(assistantRequests[1]?.body.commands).toHaveLength(0);

  await page.goto("/tasks/102");
  await expect(page.getByText("cancel-me", { exact: true })).toBeVisible();
  expect(assistantRequests).toHaveLength(2);
  await page.getByLabel("消息输入").fill("after-stop");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("after-stop", { exact: true })).toHaveCount(1);
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
  await expect.poll(() => assistantRequests.length).toBe(3);
  expect(assistantRequests[2]?.body.commands).toHaveLength(1);
  await page.reload();
  await expect(page.getByText("after-stop", { exact: true })).toHaveCount(1);
  await expect.poll(() => assistantRequests.length).toBe(3);

  const logs = await frontendLogs(page);
  expect(logs.filter((entry) => entry.event === "assistant_runtime_unmounted")).toHaveLength(0);
  const telemetry = await (await request.get("http://127.0.0.1:8000/__test__/telemetry")).json() as {
    clientCancelCount: number;
  };
  // Explicit stop is the one expected client-side cancellation path.
  expect(telemetry.clientCancelCount).toBeGreaterThan(0);
});

test("编辑入口只允许最新用户消息，并提交 sourceId 触发重跑", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 103, title: "任务103" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-selection:task:103",
      JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }),
    );
  });

  const assistantRequests: Array<{ body: Record<string, unknown>; status: number }> = [];
  page.on("response", (response) => {
    if (!response.url().endsWith("/assistant")) return;
    assistantRequests.push({ body: response.request().postDataJSON() as Record<string, unknown>, status: response.status() });
  });

  await page.goto("/tasks/103");
  const send = async (text: string) => {
    const nextRequestCount = assistantRequests.length + 1;
    await page.getByLabel("消息输入").fill(text);
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
    await expect.poll(() => assistantRequests.length).toBe(nextRequestCount);
    await expect(page.getByRole("button", { name: "发送" })).toBeVisible();
  };
  await send("first");
  await send("second");
  await expect.poll(() => assistantRequests.length).toBe(2);

  const userMessages = page.locator('[data-role="user"]');
  const followingAssistant = page.locator('[data-role="assistant"]').first();
  const followingAssistantBeforeHover = await followingAssistant.boundingBox();
  await userMessages.nth(0).hover();
  await expect(userMessages.nth(0).getByRole("button", { name: "编辑并重跑" })).toHaveCount(0);
  await expect(userMessages.nth(0).getByRole("button", { name: "复制" })).toBeVisible();
  const followingAssistantAfterFirstHover = await followingAssistant.boundingBox();
  expect(followingAssistantBeforeHover).not.toBeNull();
  expect(followingAssistantAfterFirstHover).not.toBeNull();
  expect(followingAssistantAfterFirstHover?.y).toBe(followingAssistantBeforeHover?.y);
  await userMessages.last().hover();
  const editButton = userMessages.last().getByRole("button", { name: "编辑并重跑" });
  await expect(editButton).toBeVisible();
  await expect(userMessages.last().getByRole("button", { name: "复制" })).toBeVisible();
  await editButton.click();

  const editInput = page.getByLabel("编辑消息");
  await expect(editInput).toHaveValue("second");
  await editInput.fill("second-edited");
  const rerunResponse = page.waitForResponse((response) => (
    response.url().endsWith("/assistant") && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "重跑" }).click();
  await expect((await rerunResponse).status()).toBe(200);
  await expect(page.getByText("second-edited", { exact: true })).toBeVisible();
  await expect.poll(() => assistantRequests.length).toBe(3);

  const rerunCommand = (assistantRequests[2]?.body.commands as Array<{ sourceId?: string; message?: { parts?: Array<{ text?: string }> } }>)[0];
  expect(rerunCommand?.sourceId).toBe("user-2");
  expect(rerunCommand?.message?.parts?.[0]?.text).toBe("second-edited");
  expect(assistantRequests[2]?.body.runId).toBe(2);
  expect(assistantRequests[2]?.status).toBe(200);
});

test("编辑重跑失败时恢复消息级编辑，不覆盖顶部草稿", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 104, title: "任务104" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-selection:task:104",
      JSON.stringify({ providerId: 2, modelName: "demo-model", reasoningEffort: null }),
    );
  });

  await page.goto("/tasks/104");
  const send = async (text: string) => {
    await page.getByLabel("消息输入").fill(text);
    await page.getByRole("button", { name: "发送" }).click();
    await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "发送" })).toBeVisible();
  };
  await send("first");
  await send("second");

  const topComposer = page.getByLabel("消息输入");
  await topComposer.fill("keep draft");
  const latestUser = page.locator('[data-role="user"]').last();
  await latestUser.hover();
  await latestUser.getByRole("button", { name: "编辑并重跑" }).click();
  const editInput = page.getByLabel("编辑消息");
  await editInput.fill("edit-failure");
  const failureResponse = page.waitForResponse((response) => (
    response.url().endsWith("/assistant") && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "重跑" }).click();
  await expect((await failureResponse).status()).toBe(409);

  await expect(page.getByRole("status")).toContainText("编辑重跑被测试后端拒绝");
  await expect(page.getByLabel("编辑消息")).toHaveValue("edit-failure");
  await expect(topComposer).toHaveValue("keep draft");
});
