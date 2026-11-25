import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import { expect, test } from "@playwright/test";
import { attachFirstFrame } from "./attach-first-frame";
import { backendBaseUrl, desktopFetch, launchDesktop, unstartedSession, waitForAppShell } from "./desktop-app";

type Message = { role: string; tool_call_id?: string; content?: unknown };
type Call = { name: string; id: string; args: Record<string, unknown> };
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;

for (const delivery of ["direct", "send-now", "child"] as const) {
  test(`new input replaces pending terminal approval through ${delivery}`, async () => {
    test.skip(process.platform === "win32", "Windows sandbox is outside this change");
    const { app, page, home } = await launchDesktop();
    const workspacePath = path.join(home, "workspace");
    const outside = path.join(home, "outside");
    fs.mkdirSync(workspacePath); fs.mkdirSync(outside);
    let childResumptions = 0;
    let closedOldCalls: Message[] = [];
    const terminal = (id: string, file: string, approved = false, prefix = ""): Call => ({
      id, name: "execute_terminal", args: {
        intent: approved ? "写入隔离测试目录以验证输入接管" : "验证输入接管", command: `${prefix}printf verified > ${quote(file)}`,
        shell: "auto", timeout: null, background: false, workdir: null,
        escalate_sandbox: approved ? true : null,
      },
    });
    const server = http.createServer(async (request, response) => {
      if (!request.url?.includes("chat/completions")) { response.writeHead(404).end(); return; }
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      const body = JSON.parse(Buffer.concat(chunks).toString());
      const messages = body.messages as Message[];
      const text = JSON.stringify(messages);
      const done = (id: string) => messages.some((m) => m.role === "tool" && m.tool_call_id === id);
      let calls: Call[] = [];
      let content = "Approval input test";
      if (body.tools) {
        const isChild = text.includes("CHILD_APPROVAL_INPUT") && !text.includes("start-child-approval");
        if (isChild) {
          if (done("child-old")) { childResumptions++; content = "Unexpected child resumption."; }
          else calls = [terminal("child-old", path.join(outside, "child-old.txt"), true),
                        terminal("child-remainder", path.join(workspacePath, "child-remainder.txt"))];
        } else if (text.includes("NEW_INPUT_ACCEPTED")) {
          closedOldCalls = messages.filter((m) => m.role === "tool" && String(m.tool_call_id).startsWith("old-"));
          if (done("new-command")) content = "New input handled.";
          else calls = [terminal("new-command", path.join(workspacePath, "new.txt"))];
        } else if (delivery === "child") {
          if (!done("delegate-child")) calls = [{ id: "delegate-child", name: "delegate_task", args: {
            child_agent_id: "general-purpose", agent_name: "审批测试", message: "CHILD_APPROVAL_INPUT", writable_root: null,
          } }];
          else calls = [{ id: "wait-child", name: "wait_messages", args: {} }];
        } else {
          calls = [terminal("old-approved", path.join(outside, "old.txt"), true),
                   terminal("old-remainder", path.join(workspacePath, "remainder.txt")),
                   terminal("old-second-approval", path.join(outside, "old-second.txt"), true)];
          if (delivery === "send-now") calls.unshift(terminal("old-queue-gate", path.join(workspacePath, "ready.txt"), false,
            `printf ready > ${quote(path.join(workspacePath, "queue-ready.txt"))}; sleep 3; `));
        }
      }
      const envelope = { id: "chatcmpl-input-test", created: 1, model: "input-test" };
      if (!body.stream) {
        response.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ ...envelope,
          object: "chat.completion", choices: [{ index: 0, message: { role: "assistant", content }, finish_reason: "stop" }] }));
        return;
      }
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      const event = (delta: unknown, finish_reason: string | null) => response.write(`data: ${JSON.stringify({ ...envelope,
        object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
      if (calls.length) {
        event({ role: "assistant", tool_calls: calls.map((call, index) => ({ index, id: call.id, type: "function",
          function: { name: call.name, arguments: JSON.stringify(call.args) } })) }, null);
        event({}, "tool_calls");
      } else { event({ role: "assistant", content }, null); event({}, "stop"); }
      response.end("data: [DONE]\n\n");
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    try {
      await waitForAppShell(page);
      const base = await backendBaseUrl(page);
      const workspaceResponse = await desktopFetch(`${base}/workspaces`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "输入接管验收", root_path: workspacePath }) });
      expect(workspaceResponse.status).toBe(200);
      const workspace = await workspaceResponse.json();
      const session = await unstartedSession(base, workspace.workspace_id);
      const models = await desktopFetch(`${base}/configuration/model-connections`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "Approval input E2E", base_url: `http://127.0.0.1:${(server.address() as { port: number }).port}/v1`,
          api_key: "isolated-local-test", models: [{ model_name: "input-test", context_window_k: 64, supports_reasoning_effort: true }] }) });
      expect(models.status, await models.clone().text()).toBe(200);
      await page.goto(`app://cosir/sessions/${session}`);
      const input = page.getByLabel("消息输入");
      await input.fill(delivery === "child" ? "start-child-approval" : "start-approval");
      // 首条消息点「发送」：模型目录还在加载时按钮禁用，点击会自动等到它可用（回车不会等）。
      await page.getByRole("button", { name: "发送", exact: true }).click();
      const card = page.getByRole("region", { name: "终端执行批准" });
      if (delivery === "send-now") {
        await expect.poll(() => fs.existsSync(path.join(workspacePath, "queue-ready.txt")), { timeout: 30_000 }).toBe(true);
        await input.fill("NEW_INPUT_ACCEPTED"); await input.press("Enter");
        await expect(page.getByLabel("立即发送")).toBeVisible();
      }
      await expect(card).toBeVisible({ timeout: 30_000 });
      const before = await attachFirstFrame(base, session);
      const oldRequest = before.terminal_approvals[0];
      expect(fs.existsSync(path.join(outside, delivery === "child" ? "child-old.txt" : "old.txt"))).toBe(false);
      if (delivery === "child") expect(oldRequest.originSessionId).not.toBe(session);
      await card.screenshot({ path: path.join(home, `approval-${delivery}-before.png`) });
      if (delivery === "send-now") await page.getByLabel("立即发送").click();
      else { await input.fill("NEW_INPUT_ACCEPTED"); await input.press("Enter"); }
      await expect(card).toHaveCount(0);
      await expect(page.getByText("New input handled.", { exact: true })).toBeVisible({ timeout: 30_000 });
      await expect.poll(() => fs.existsSync(path.join(workspacePath, "new.txt"))).toBe(true);
      const stale = await desktopFetch(`${base}/sessions/${session}/terminal-approvals/${oldRequest.id}/decision`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: "approve" }) });
      expect(stale.status).toBe(409);
      for (const file of [path.join(outside, "old.txt"), path.join(outside, "old-second.txt"), path.join(outside, "child-old.txt"),
        path.join(workspacePath, "remainder.txt"), path.join(workspacePath, "child-remainder.txt")]) expect(fs.existsSync(file)).toBe(false);
      const after = await attachFirstFrame(base, session);
      expect(after.activity?.startedAt).toBe(before.activity?.startedAt);
      expect(after.terminal_permission_mode).toBe("request_approval");
      expect(after.terminal_approvals).toEqual([]);
      expect(after.queued_messages ?? []).toEqual([]);
      if (delivery !== "child") {
        expect(closedOldCalls).toHaveLength(delivery === "send-now" ? 4 : 3);
        for (const message of closedOldCalls.filter((m) => m.tool_call_id !== "old-queue-gate")) expect(String(message.content)).toContain("cancelled:");
      } else expect(childResumptions).toBe(0);
      await page.screenshot({ path: path.join(home, `approval-${delivery}-after.png`) });
      fs.writeFileSync(path.join(home, "approval-input-evidence.json"), JSON.stringify({ delivery, oldRequestId: oldRequest.id,
        beforeStartedAt: before.activity?.startedAt, afterStartedAt: after.activity?.startedAt,
        childResumptions, closedOldCallIds: closedOldCalls.map((m) => m.tool_call_id), newFile: fs.readFileSync(path.join(workspacePath, "new.txt"), "utf8") }, null, 2));
      console.log(`approval input evidence: ${home}`);
    } finally {
      await app.close(); server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
}
