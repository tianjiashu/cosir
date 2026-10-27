import { expect, test } from "@playwright/test";

type FrontendLog = {
  event?: string;
  data?: Record<string, unknown>;
};

/** Task 级业务命令端点：发送、编辑、恢复与用户决定（attach / state 不算命令）。 */
const TASK_COMMAND_PATH = /\/tasks\/\d+\/(?:messages(?:\/[^/]+\/rerun)?|resume|tool-calls\/[^/]+\/decision)$/;
function isTaskCommand(url: string): boolean {
  return TASK_COMMAND_PATH.test(new URL(url).pathname);
}

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
});

test("新对话 Assistant Transport 被拒绝时不导航、不丢输入并清理 provisional Task", async ({ page, request }) => {
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:workspace:7",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });
  await page.route("http://127.0.0.1:8000/tasks/*/messages", async (route) => {
    await route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({ detail: { error: { code: "INVALID_MESSAGE", message: "测试拒绝新对话", retryable: true } } }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: "选择工作区" }).click();
  await page.getByRole("option", { name: /demo/ }).click();
  await expect(page.getByRole("combobox", { name: "选择模型" })).toContainText("demo-model");
  const input = page.getByLabel("新对话内容");
  await input.fill("保留这段输入");
  await page.getByRole("button", { name: "开始对话" }).click();

  await expect(page).toHaveURL("http://127.0.0.1:4173/");
  await expect(page.getByRole("dialog")).toContainText("测试拒绝新对话");
  await expect(input).toHaveText("保留这段输入");
  await expect.poll(async () => (await request.get("http://127.0.0.1:8000/workspaces/7/tasks")).json()).toEqual([]);
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
      "cosir:model-config-selection:workspace:7",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });
  const assistantRequests: Array<{ url: string; body: Record<string, unknown>; status: number }> = [];
  page.on("response", (response) => {
    if (!isTaskCommand(response.url())) return;
    const body = (response.request().postDataJSON() ?? {}) as Record<string, unknown>;
    assistantRequests.push({ url: response.url(), body, status: response.status() });
  });
  await page.route("http://127.0.0.1:8000/model-configs", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify([{
        config_id: 1,
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
      }]),
    });
  });

  await page.goto("/");
  await expect(page.getByText("demo").first()).toBeVisible();
  await page.getByRole("button", { name: "选择工作区" }).click();
  await page.getByRole("option", { name: /demo/ }).click();
  await expect(page.getByRole("combobox", { name: "选择模型" })).toContainText("demo-model");
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("cosir:model-config-selection:workspace:7"))).toBe(
    JSON.stringify({ modelConfigId: 1, reasoningEffort: null }),
  );
  expect(await page.evaluate(() => window.localStorage.getItem("cosir:model-config-selection:default"))).toBeNull();
  await page.getByRole("button", { name: "工具", exact: true }).click();
  const fileToolGroup = page.getByRole("switch", { name: "文件", exact: true });
  await expect(fileToolGroup).toBeChecked();
  await fileToolGroup.click();
  await expect(fileToolGroup).not.toBeChecked();
  await expect(page.getByRole("button", { name: "工具，已关闭 1 组" })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByLabel("新对话内容").fill("你好");
  await expect(page.getByRole("button", { name: "开始对话" })).toBeEnabled();
  await page.getByRole("button", { name: "开始对话" }).click();

  await expect(page).toHaveURL(/\/tasks\/42$/);
  await expect.poll(() => assistantRequests.length).toBe(1);
  // 创建后的自动目录同步必须显示新任务，且不能替换 Assistant 会话或中断流。
  await expect(page.getByRole("navigation", { name: "工作区任务列表" }).getByRole("treeitem").filter({ hasText: "你好" })).toBeVisible();
  await expect(page.getByRole("button", { name: "刷新工作区" })).toHaveCount(0);
  expect(assistantRequests[0]?.status).toBe(202);
  expect(assistantRequests[0]?.url).toMatch(/\/tasks\/42\/messages$/);
  expect(assistantRequests[0]?.body).toMatchObject({
    message: { role: "user", parts: [{ type: "text", text: "你好" }] },
    modelConfigId: 1,
    banTools: ["read_file"],
    delivery: "immediate",
  });
  await expect(page.getByRole("button", { name: "工具，已关闭 1 组" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("cosir:tool-group-selection:task:42"))).toBe(
    JSON.stringify(["文件"]),
  );
  await expect(page.locator('[data-role="user"]').getByText("你好", { exact: true })).toBeVisible();
  await expect(page.getByText("streaming response", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("思考过程", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible();

  const messageInput = page.getByLabel("消息输入");
  const secondResponsePromise = page.waitForResponse((response) => (
    response.url().endsWith("/tasks/42/messages") && response.request().method() === "POST"
  ));
  await messageInput.fill("请继续");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
  await secondResponsePromise;
  const streamLogUrl = "http://127.0.0.1:8000/__test__/last-stream";
  // 订阅不随 Run 结束关闭：以第二轮回复写入结局作为本轮输出结束。
  await expect.poll(async () => (await request.get(streamLogUrl)).text()).toContain('"runId":2,"outcome":{"status":"completed"');
  const streamBody = await (await request.get(streamLogUrl)).text();
  await expect.poll(() => assistantRequests.length).toBe(2);
  expect(assistantRequests[1]?.status).toBe(202);
  expect(assistantRequests[1]?.url).toMatch(/\/tasks\/42\/messages$/);
  expect(assistantRequests[1]?.body.state).toBeUndefined();
  expect(assistantRequests[1]?.body.banTools).toEqual(["read_file"]);
  expect(streamBody).toContain('"kind":"mutation"');
  expect(streamBody).toContain('"kind":"append-text"');
  expect(streamBody).toContain("streaming response");

  const logs = await frontendLogs(page);
  expect(logs.filter((entry) => entry.event === "assistant_runtime_unmounted")).toHaveLength(0);
  // Task 页面只建立一条长订阅：两轮输出都经它到达，命令返回不再重新 attach。
  expect(logs.filter((entry) => entry.event === "assistant_transport_subscription_opened")).toHaveLength(1);
  expect(logs.filter((entry) => entry.event === "assistant_transport_subscription_closed")).toHaveLength(0);
  const settled = logs.filter((entry) => entry.event === "assistant_activity_settled");
  expect(settled.length).toBeGreaterThanOrEqual(1);
  expect(settled.at(-1)?.data?.status).toBe("completed");
  const restoredPage = await page.context().newPage();
  await restoredPage.goto("/tasks/42");
  await expect(restoredPage.getByRole("button", { name: "工具，已关闭 1 组" })).toBeVisible();
  await restoredPage.close();
  const telemetry = await (await request.get("http://127.0.0.1:8000/__test__/telemetry")).json() as {
    clientCancelCount: number;
  };
  // 命令只投递，订阅常驻：运行期间客户端从未主动断开订阅。
  expect(telemetry.clientCancelCount).toBe(0);
});

test("任务页面重挂载时自动恢复未结束的 run", async ({ page, request }) => {
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:task:42",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });

  await request.post("http://127.0.0.1:8000/__test__/seed-running", {
    data: { text: "恢复测试" },
  });

  const resumeResponse = page.waitForResponse((response) => (
    response.url().endsWith("/tasks/42/assistant/attach") && response.request().method() === "POST"
  ));
  await page.goto("/tasks/42");
  await expect(page.locator('[data-role="user"]').getByText("恢复测试", { exact: true })).toBeVisible();
  await expect(page.getByTestId("run-usage-display")).toHaveText("用量统计中…");
  await resumeResponse;
  await expect(page.getByText("resumed", { exact: true })).toBeVisible();
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  await expect.poll(async () => (await request.get("http://127.0.0.1:8000/__test__/last-stream")).text()).toContain('"outcome":{"status":"completed"');
});

test("不同 task 的 Assistant Transport 会话与消息互相隔离", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 100, title: "任务100" } });
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 101, title: "任务101" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:task:100",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });

  await page.goto("/tasks/100");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByLabel("消息输入").fill("task-100-message");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("task-100-message", { exact: true })).toBeVisible();
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();

  await page.goto("/tasks/101");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await expect(page.getByText("task-100-message", { exact: true })).toHaveCount(0);
  await page.getByLabel("消息输入").fill("task-101-message");
  await page.getByRole("button", { name: "发送", exact: true }).click();
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
      "cosir:model-config-selection:task:102",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });

  const assistantRequests: Array<{ url: string; body: Record<string, unknown>; status: number }> = [];
  const assistantStateRequests: string[] = [];
  const attachRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/assistant/state")) assistantStateRequests.push(request.url());
    if (request.url().endsWith("/assistant/attach") && request.method() === "POST") attachRequests.push(request.url());
  });
  page.on("response", (response) => {
    if (!isTaskCommand(response.url())) return;
    assistantRequests.push({
      url: response.url(),
      body: (response.request().postDataJSON() ?? {}) as Record<string, unknown>,
      status: response.status(),
    });
  });

  await page.goto("/tasks/102");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await expect.poll(() => attachRequests.length).toBe(1);
  const stateReadsBeforeCancel = assistantStateRequests.length;
  await page.getByLabel("消息输入").fill("cancel-me");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("stream", { exact: true })).toBeVisible();
  const cancelResponse = page.waitForResponse((response) => (
    /\/tasks\/\d+\/cancel$/.test(new URL(response.url()).pathname)
    && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "停止" }).click();
  await expect((await cancelResponse).status()).toBe(202);
  await expect(page.getByRole("button", { name: "继续运行" })).toBeVisible();
  // 停止是 Task 命令：不关闭订阅、不轮询 state，终态经同一条长订阅到达。
  expect(assistantStateRequests.length).toBe(stateReadsBeforeCancel);
  expect(attachRequests).toHaveLength(1);
  expect(assistantRequests).toHaveLength(1);

  // A cold Assistant runtime must derive the same business-resume action from the
  // canonical cancelled snapshot, rather than relying on the optimistic local state.
  await page.reload();
  await expect(page.getByText("cancel-me", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "继续运行" })).toBeVisible();
  await expect.poll(() => attachRequests.length).toBe(2);

  const resumeResponse = page.waitForResponse((response) => (
    response.url().endsWith("/tasks/102/resume") && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "继续运行" }).click();
  await expect((await resumeResponse).status()).toBe(202);
  await expect(page.getByText("resumed response", { exact: true })).toBeVisible();
  // 业务恢复只投递命令：续跑输出经已有订阅到达，不再额外 attach。
  expect(attachRequests).toHaveLength(2);
  await expect.poll(() => assistantRequests.length).toBe(2);
  expect(assistantRequests[1]?.url).toMatch(/\/tasks\/102\/resume$/);

  await page.goto("/tasks/102");
  await expect(page.getByText("cancel-me", { exact: true })).toBeVisible();
  expect(assistantRequests).toHaveLength(2);
  await page.getByLabel("消息输入").fill("after-stop");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("after-stop", { exact: true })).toHaveCount(1);
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
  await expect.poll(() => assistantRequests.length).toBe(3);
  expect(assistantRequests[2]?.url).toMatch(/\/tasks\/102\/messages$/);
  await page.reload();
  await expect(page.getByText("after-stop", { exact: true })).toHaveCount(1);
  await expect.poll(() => assistantRequests.length).toBe(3);

  const logs = await frontendLogs(page);
  expect(logs.filter((entry) => entry.event === "assistant_runtime_unmounted")).toHaveLength(0);
});

test("不可恢复时继续运行被调度拒绝，顶层错误显示在对话区", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 105, title: "拒绝恢复" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:task:105",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });
  await page.goto("/tasks/105");
  await page.getByLabel("消息输入").fill("cancel-then-reject");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("stream", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "停止" }).click();
  await expect(page.getByRole("button", { name: "继续运行" })).toBeVisible();

  // 让 Run 不再是用户取消：模拟后端判定不可恢复，拒绝以顶层 error 投影回来。
  const state = await (await request.get("http://127.0.0.1:8000/tasks/105/assistant/state")).json();
  state.activity.endReason = "max_steps_reached";
  await request.post("http://127.0.0.1:8000/__test__/seed-state", { data: { taskId: 105, state } });
  await page.getByRole("button", { name: "继续运行" }).click();
  await expect(page.getByText("当前运行不可恢复", { exact: true })).toBeVisible();
  // 被拒即复位按钮，不等 15 秒的无响应超时（expect 默认 5 秒内必须完成）。
  await expect(page.getByRole("button", { name: "继续运行" })).toBeEnabled();

  // 同样的原因再被拒一次：错误值只差 seq，按钮照样复位。
  await page.getByRole("button", { name: "继续运行" }).click();
  await expect(page.getByRole("button", { name: "正在继续运行" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "继续运行" })).toBeEnabled();
  const logs = await frontendLogs(page);
  expect(logs.filter((entry) => entry.event === "composer_resume_rejected")).toHaveLength(2);
});

test("取消后订阅断开时，前端有限重连并解除停止状态", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 103, title: "取消流断开" } });
  await request.post("http://127.0.0.1:8000/__test__/drop-next-cancel-stream");
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:task:103",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });

  const attachRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/assistant/attach") && request.method() === "POST") attachRequests.push(request.url());
  });

  await page.goto("/tasks/103");
  await expect(page.getByLabel("消息输入")).toBeVisible();
  await page.getByLabel("消息输入").fill("cancel-with-dropped-stream");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("stream", { exact: true })).toBeVisible();
  const attachesBeforeCancel = attachRequests.length;
  const cancelResponse = page.waitForResponse((response) => (
    /\/tasks\/\d+\/cancel$/.test(new URL(response.url()).pathname)
    && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "停止" }).click();
  await expect((await cancelResponse).status()).toBe(202);

  // 订阅意外断开进入有限重连：重开订阅的 full 帧带回已取消的终态。
  await expect.poll(() => attachRequests.length).toBeGreaterThan(attachesBeforeCancel);
  await expect(page.getByRole("button", { name: "继续运行" })).toBeVisible();
  await expect(page.getByRole("button", { name: "正在停止" })).toHaveCount(0);
});

test("编辑入口只允许最新用户消息，并提交 sourceId 触发重跑", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 103, title: "任务103" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:task:103",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });

  const assistantRequests: Array<{ url: string; body: Record<string, unknown>; status: number }> = [];
  page.on("response", (response) => {
    if (!isTaskCommand(response.url())) return;
    assistantRequests.push({ url: response.url(), body: (response.request().postDataJSON() ?? {}) as Record<string, unknown>, status: response.status() });
  });

  await page.goto("/tasks/103");
  const send = async (text: string) => {
    const nextRequestCount = assistantRequests.length + 1;
    await page.getByLabel("消息输入").fill(text);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
    await expect.poll(() => assistantRequests.length).toBe(nextRequestCount);
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible();
  };
  await send("first");
  await send("second");
  await expect.poll(() => assistantRequests.length).toBe(2);

  const userMessages = page.locator('[data-role="user"]');
  const followingAssistant = page.locator('[data-role="assistant"]').first();
  // 先滚到可见再测量：否则 hover 自带的 scrollIntoView 会被误判为悬停导致的布局位移。
  await userMessages.nth(0).scrollIntoViewIfNeeded();
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

  const editInput = page.getByRole("textbox", { name: "编辑消息" });
  await expect(editInput).toHaveText("second");
  await editInput.fill("second-edited");
  const rerunResponse = page.waitForResponse((response) => (
    response.url().endsWith("/messages/user-2/rerun") && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "重跑" }).click();
  await expect((await rerunResponse).status()).toBe(202);
  await expect(page.getByText("second-edited", { exact: true })).toBeVisible();
  await expect.poll(() => assistantRequests.length).toBe(3);

  // 编辑按消息寻址：目标在路径里，请求体不携带 runId 或投递方式。
  expect(assistantRequests[2]?.url).toMatch(/\/tasks\/103\/messages\/user-2\/rerun$/);
  expect(assistantRequests[2]?.body).toMatchObject({ message: { parts: [{ type: "text", text: "second-edited" }] } });
  expect(assistantRequests[2]?.body.runId).toBeUndefined();
  expect(assistantRequests[2]?.body.delivery).toBeUndefined();
  expect(assistantRequests[2]?.status).toBe(202);
});

test("编辑重跑失败时恢复消息级编辑，不覆盖顶部草稿", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 104, title: "任务104" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:task:104",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });

  await page.goto("/tasks/104");
  const send = async (text: string) => {
    await page.getByLabel("消息输入").fill(text);
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeVisible();
  };
  await send("first");
  await send("second");

  const topComposer = page.getByLabel("消息输入");
  await topComposer.fill("keep draft");
  const latestUser = page.locator('[data-role="user"]').last();
  await latestUser.hover();
  await latestUser.getByRole("button", { name: "编辑并重跑" }).click();
  const editInput = page.getByRole("textbox", { name: "编辑消息" });
  await editInput.fill("edit-failure");
  const failureRequest = page.waitForRequest((request) => (
    request.url().endsWith("/tasks/104/messages/user-2/rerun") && request.method() === "POST"
  ));
  const failureResponse = page.waitForResponse((response) => (
    response.url().endsWith("/tasks/104/messages/user-2/rerun") && response.request().method() === "POST"
  ));
  await page.getByRole("button", { name: "重跑" }).click();
  const failedRequestBody = (await failureRequest).postDataJSON() as {
    message?: { parts?: Array<{ type?: string; text?: string }> };
  };
  expect(failedRequestBody.message?.parts?.find((part) => part.type === "text")?.text).toBe("edit-failure");
  await expect((await failureResponse).status()).toBe(400);

  await expect(page.getByRole("dialog")).toContainText("编辑重跑被测试后端拒绝");
  await page.getByRole("dialog").getByRole("button", { name: "关闭" }).click();
  await expect(page.getByRole("textbox", { name: "编辑消息" })).toHaveText("edit-failure");
  await expect(topComposer).toHaveText("keep draft");
});

test("attach 被拒后进入有限重连，订阅恢复后继续接收输出", async ({ page, request }) => {
  await request.post("http://127.0.0.1:8000/__test__/seed-task", { data: { taskId: 106, title: "重连" } });
  await page.addInitScript(() => {
    window.localStorage.clear();
    window.localStorage.setItem(
      "cosir:model-config-selection:task:106",
      JSON.stringify({ modelConfigId: 2, reasoningEffort: null }),
    );
  });
  const attachStatuses: number[] = [];
  page.on("response", (response) => {
    if (response.url().endsWith("/tasks/106/assistant/attach")) attachStatuses.push(response.status());
  });
  await request.post("http://127.0.0.1:8000/__test__/fail-next-attach");
  await page.goto("/tasks/106");
  await expect.poll(() => attachStatuses).toEqual([503, 200]);
  await page.getByLabel("消息输入").fill("after-reconnect");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(page.getByText("streaming response", { exact: true })).toBeVisible();
  expect(attachStatuses).toEqual([503, 200]);
});
