import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import { expect, test } from "@playwright/test";
import { backendBaseUrl, desktopFetch, launchDesktop, unstartedSession, waitForAppShell } from "./desktop-app";
import { attachFirstFrame } from "./attach-first-frame";

/** 只替换模型 HTTP 服务；Electron、后端、批准界面和 Rust 命令均为真实实现。 */
test("approves the exact command, runs it in Rust and switches the next run to full access", async () => {
  test.skip(process.platform === "win32", "Windows sandbox is outside this change");
  const { app, page, home } = await launchDesktop();
  const outside = path.join(home, "outside");
  fs.mkdirSync(outside);
  const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
  const server = http.createServer(async (request, response) => {
    if (!request.url?.includes("chat/completions")) { response.writeHead(404).end(); return; }
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(chunks).toString());
    const reject = JSON.stringify(body.messages).includes("reject-test");
    const full = !reject && JSON.stringify(body.messages).includes("full-test");
    const callId = reject ? "reject-call" : full ? "full-call" : "approval-call";
    const finished = body.messages.some((message: { role: string; tool_call_id?: string }) => message.role === "tool" && message.tool_call_id === callId);
    const content = reject ? "Rejection handled." : full ? "Full access verified." : "Sandbox verified.";
    const envelope = { id: "chatcmpl-local-test", created: 1, model: "sandbox-test" };
    if (!body.stream) {
      response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ ...envelope,
        object: "chat.completion", choices: [{ index: 0, message: { role: "assistant", content: "Sandbox test" }, finish_reason: "stop" }] }));
      return;
    }
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    const event = (delta: unknown, finish_reason: string | null) => response.write(`data: ${JSON.stringify({ ...envelope, object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
    if (!body.tools || finished) {
      event({ role: "assistant", content: body.tools ? content : "Sandbox test" }, null);
      event({}, "stop");
    } else {
      const args = { intent: "验证终端权限", command: `printf verified > ${quote(path.join(outside, reject ? "rejected.txt" : full ? "full.txt" : "approved.txt"))}`,
        shell: "auto", background: false, timeout: null, workdir: null,
        permissions: full ? null : { network: false, write_roots: [outside] }, justification: full ? "" : "本次测试需要写入工作区外的临时目录" };
      event({ role: "assistant", tool_calls: [{ index: 0, id: callId, type: "function", function: { name: "execute_terminal", arguments: JSON.stringify(args) } }] }, null);
      event({}, "tool_calls");
    }
    response.end("data: [DONE]\n\n");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const provider = `http://127.0.0.1:${(server.address() as { port: number }).port}/v1`;
  try {
    await waitForAppShell(page);
    const base = await backendBaseUrl(page);
    const workspacePath = path.join(home, "workspace"); fs.mkdirSync(workspacePath);
    const workspaceResponse = await desktopFetch(`${base}/workspaces`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "沙箱验收", root_path: workspacePath }) });
    expect(workspaceResponse.status).toBe(200);
    const workspace = await workspaceResponse.json();
    const session = await unstartedSession(base, workspace.workspace_id);
    const models = await desktopFetch(`${base}/configuration/model-connections`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "Sandbox E2E", base_url: provider, api_key: "isolated-local-test", models: [{ model_name: "sandbox-test", context_window_k: 64, supports_reasoning_effort: true }] }) });
    expect(models.status, await models.clone().text()).toBe(200);
    const model = (await models.json()).models[0].config_id;
    await page.goto(`app://cosir/sessions/${session}`);
    await expect(page.getByLabel("终端权限")).toHaveValue("request_approval");
    await page.getByLabel("消息输入").fill("request-test");
    await page.getByLabel("消息输入").press("Enter");
    const card = page.getByRole("region", { name: "终端执行批准" });
    await expect(card).toBeVisible({ timeout: 30_000 });
    expect(fs.existsSync(path.join(outside, "approved.txt"))).toBe(false);
    await expect(card).toContainText(outside);
    await expect(page.getByLabel("终端权限")).toBeDisabled();
    const snapshot = await attachFirstFrame(base, session);
    const requestId = snapshot.terminal_approvals[0].id;
    // 原生 HTTP 客户端不受 CORS 限制，仍必须无法代替用户批准。
    const forged = await fetch(`${base}/sessions/${session}/terminal-approvals/${requestId}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "approve" }) });
    expect(forged.status).toBe(403);
    await card.screenshot({ path: path.join(home, "terminal-approval.png") });
    await card.getByRole("button", { name: "允许一次" }).click();
    await expect.poll(() => fs.existsSync(path.join(outside, "approved.txt"))).toBe(true);
    await expect(page.getByText("Sandbox verified.", { exact: true })).toBeVisible();
    expect(fs.readFileSync(path.join(outside, "approved.txt"), "utf8")).toBe("verified");
    const duplicate = await desktopFetch(`${base}/sessions/${session}/terminal-approvals/${requestId}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "approve" }) });
    expect(duplicate.status).toBe(409);
    await expect(page.getByLabel("终端权限")).toBeEnabled();
    await page.getByLabel("终端权限").selectOption("full_access");
    await expect(page.getByLabel("终端权限")).toHaveValue("full_access");
    await page.getByLabel("消息输入").fill("full-test");
    await page.getByLabel("消息输入").press("Enter");
    await expect.poll(() => fs.existsSync(path.join(outside, "full.txt"))).toBe(true);
    await expect(page.getByText("Full access verified.", { exact: true })).toBeVisible();
    await expect(card).toHaveCount(0);
    const after = await attachFirstFrame(base, session);
    expect(after.activity?.launch.terminalPermissionMode).toBe("full_access");
    await expect(page.getByLabel("终端权限")).toBeEnabled();
    await page.getByLabel("终端权限").selectOption("request_approval");
    await expect(page.getByLabel("终端权限")).toHaveValue("request_approval");
    await page.getByLabel("消息输入").fill("reject-test");
    await page.getByLabel("消息输入").press("Enter");
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "拒绝", exact: true }).click();
    await expect(page.getByText("Rejection handled.", { exact: true })).toBeVisible();
    await expect(card).toHaveCount(0);
    expect(fs.existsSync(path.join(outside, "rejected.txt"))).toBe(false);
    expect(model).toBeTruthy();
  } finally {
    await app.close();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});
