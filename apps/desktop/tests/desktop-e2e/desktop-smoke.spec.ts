import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

import { backendBaseUrl, launchDesktop, openNewConversation, unstartedSession } from "./desktop-app";
import { attachFirstFrame } from "./attach-first-frame";

test("starts the host, reaches the workspace shell and isolates its data under the given home", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page).toHaveTitle("Cosir");
    await expect(page.getByRole("navigation", { name: "工作区会话列表" })).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText("无法连接本机后端")).toHaveCount(0);
    expect(await backendBaseUrl(page)).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    expect(fs.existsSync(path.join(home, ".cosir", "storage"))).toBe(true);
  } finally {
    await app.close();
  }
});

/** 读出临时数据根下全部后端结构化日志行里的事件名（含同日大小分片）。 */
function backendLogEvents(home: string): string[] {
  const logDir = path.join(home, ".cosir", "logs");
  if (!fs.existsSync(logDir)) return [];
  return fs.readdirSync(logDir)
    .filter((name) => name.startsWith("backend-") && !name.startsWith("backend-console") && name.endsWith(".log"))
    .flatMap((name) => fs.readFileSync(path.join(logDir, name), "utf8").split("\n"))
    .filter(Boolean)
    .map((line) => {
      try {
        return String((JSON.parse(line) as { event?: unknown }).event ?? "");
      } catch {
        return "";
      }
    });
}

// 宿主工具链路：宿主用注入后端的同一个 token 连上 `/host/link`；token 不一致时后端只会记 host_link_rejected。
test("the host connects its tool link to the backend", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区会话列表" })).toBeVisible({ timeout: 120_000 });
    await expect.poll(() => backendLogEvents(home).includes("host_link_connected"), { timeout: 15_000 }).toBe(true);
    expect(backendLogEvents(home)).not.toContain("host_link_rejected");
  } finally {
    await app.close();
  }
});

test("closing the main window only hides it; the backend keeps running", async () => {
  const { app, page } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区会话列表" })).toBeVisible({ timeout: 120_000 });
    const baseUrl = await backendBaseUrl(page);

    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());

    await expect.poll(() => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map((item) => item.isVisible()))).toEqual([false]);
    expect((await fetch(`${baseUrl}/health`)).status).toBe(200);
  } finally {
    await app.close();
  }
});

test("quitting stops the backend process tree", async () => {
  const { app, page } = await launchDesktop();
  await expect(page.getByRole("navigation", { name: "工作区会话列表" })).toBeVisible({ timeout: 120_000 });
  const baseUrl = await backendBaseUrl(page);

  await app.evaluate(({ app: electronApp }) => electronApp.quit());
  await app.waitForEvent("close", { timeout: 30_000 });

  await expect.poll(async () => {
    try {
      await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(500) });
      return "alive";
    } catch {
      return "stopped";
    }
  }, { timeout: 10_000 }).toBe("stopped");
});

test("opens and reloads a conversation through the real attach first frame", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区会话列表" })).toBeVisible({ timeout: 120_000 });
    const base = await backendBaseUrl(page);
    const workspacePath = path.join(home, "workspace");
    fs.mkdirSync(workspacePath);
    const workspaceResponse = await fetch(`${base}/workspaces`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "HTTP 回归", root_path: workspacePath }),
    });
    expect(workspaceResponse.status).toBe(200);
    const workspace = await workspaceResponse.json() as { workspace_id: number };
    const session = await unstartedSession(base, workspace.workspace_id);
    expect((await attachFirstFrame(base, session)).messages).toEqual([]);
    expect((await fetch(`${base}/sessions/${session}/assistant/messages?end=0`)).status).toBe(200);
    await page.reload();
    await openNewConversation(page, "HTTP 回归", session);
    await expect(page.getByRole("textbox")).toBeVisible();
    await expect(page.getByText(/后端内部错误/)).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole("textbox")).toBeVisible();
    await expect(page.getByText(/后端内部错误/)).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath("real-snapshot-conversation.png") });
  } finally {
    await app.close();
  }
});
