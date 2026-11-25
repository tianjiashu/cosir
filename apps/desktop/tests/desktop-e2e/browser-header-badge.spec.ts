import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import type { AddressInfo } from "node:net";
import { expect, test } from "@playwright/test";

import { backendBaseUrl, desktopFetch, launchDesktop, unstartedSession, waitForAppShell } from "./desktop-app";

type Message = { role: string; tool_call_id?: string };

/**
 * 页头浏览器入口的两段式提醒，真实宿主 + 后端 + 本机假模型：Agent 经工具开标签时线在呼吸，停手后留下没看过的
 * 标签时静止，打开面板即清；面板不会被自动展开。
 */
test("Agent 用浏览器时页头入口先呼吸、停手后静止，打开面板即清", async () => {
  const testInfo = test.info();
  const page_ = http.createServer((_request, response) => {
    response.end("<title>提醒验收页</title><h1>Agent 打开的页面</h1>");
  });
  await new Promise<void>((resolve) => page_.listen(0, "127.0.0.1", resolve));
  const pageUrl = `http://127.0.0.1:${(page_.address() as AddressInfo).port}/`;

  const model = http.createServer(async (request, response) => {
    if (!request.url?.includes("chat/completions")) { response.writeHead(404).end(); return; }
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(chunks).toString());
    const messages = body.messages as Message[];
    const done = (id: string) => messages.some((m) => m.role === "tool" && m.tool_call_id === id);
    let call: { id: string; name: string; args: Record<string, unknown> } | null = null;
    if (body.tools) {
      if (!done("load-browser")) call = { id: "load-browser", name: "tool_search", args: { query: "select:browser_navigate", max_results: 1 } };
      else if (!done("open-page")) call = { id: "open-page", name: "browser_navigate", args: { url: pageUrl, new_tab: true } };
      // 停手后再答复：让「呼吸」撑完、落到「静止」时 Run 仍可能在跑，提醒只看浏览器事实。
      else await new Promise((resolve) => setTimeout(resolve, 3000));
    }
    const envelope = { id: "chatcmpl-badge", created: 1, model: "badge-test" };
    if (!body.stream) {
      response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ ...envelope, object: "chat.completion",
        choices: [{ index: 0, message: { role: "assistant", content: "页面已打开。" }, finish_reason: "stop" }] }));
      return;
    }
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    const event = (delta: unknown, finish: string | null) => response.write(`data: ${JSON.stringify({ ...envelope,
      object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`);
    if (call) {
      event({ role: "assistant", tool_calls: [{ index: 0, id: call.id, type: "function", function: { name: call.name, arguments: JSON.stringify(call.args) } }] }, null);
      event({}, "tool_calls");
    } else { event({ role: "assistant", content: "页面已打开。" }, null); event({}, "stop"); }
    response.end("data: [DONE]\n\n");
  });
  await new Promise<void>((resolve) => model.listen(0, "127.0.0.1", resolve));

  const { app, page, home } = await launchDesktop();
  try {
    await waitForAppShell(page);
    const base = await backendBaseUrl(page);
    const root = path.join(home, "workspace");
    fs.mkdirSync(root);
    const workspace = await (await desktopFetch(`${base}/workspaces`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "提醒验收", root_path: root }) })).json() as { workspace_id: number };
    const session = await unstartedSession(base, workspace.workspace_id);
    const models = await desktopFetch(`${base}/configuration/model-connections`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Badge E2E", base_url: `http://127.0.0.1:${(model.address() as AddressInfo).port}/v1`,
        api_key: "isolated-local-test", models: [{ model_name: "badge-test", context_window_k: 64, supports_reasoning_effort: true }] }) });
    expect(models.status, await models.clone().text()).toBe(200);
    await page.goto(`app://cosir/sessions/${session}`);

    const globe = page.getByRole("button", { name: /^浏览器，/ });
    const line = globe.locator("[data-status-line]");
    await expect(line).toHaveCount(0);
    await page.getByLabel("消息输入").fill("打开验收页");
    // 首条消息点「发送」：模型目录还在加载时按钮禁用，点击会自动等到它可用（回车不会等）。
    await page.getByRole("button", { name: "发送", exact: true }).click();

    await expect(globe.locator('[data-status-line="breathing"]')).toBeVisible({ timeout: 30_000 });
    await expect(globe).toHaveAccessibleName(/Agent 正在使用浏览器/);
    await globe.screenshot({ path: testInfo.outputPath("badge-breathing.png") });
    // 面板不会被自动展开。
    await expect(page.getByRole("complementary", { name: "浏览器", exact: true })).toHaveCount(0);

    await expect(globe.locator('[data-status-line="static"]')).toBeVisible({ timeout: 15_000 });
    await expect(globe).toHaveAccessibleName(/Agent 打开了 1 个标签页/);
    await globe.screenshot({ path: testInfo.outputPath("badge-static.png") });
    await page.screenshot({ path: testInfo.outputPath("badge-static-window.png") });

    await globe.click();
    const panel = page.getByRole("complementary", { name: "浏览器", exact: true });
    await expect(panel.getByRole("tab", { name: "提醒验收页" })).toBeVisible();
    await expect(line).toHaveCount(0);
    // 收起后看过的标签不再提醒。
    await globe.click();
    await expect(panel).toHaveCount(0);
    await expect(line).toHaveCount(0);
  } finally {
    await app.close();
    for (const server of [model, page_]) {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  }
});
