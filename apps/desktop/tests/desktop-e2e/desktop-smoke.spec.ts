import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";

const desktopRoot = path.resolve(__dirname, "..", "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const backendPython = path.join(repositoryRoot, "apps", "backend", process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");

/**
 * 启动真实宿主。数据根指向临时目录（`CODING_AGENT_DATA_DIR`）：SQLite、日志与运行期文件全部隔离，不碰开发者的真实数据。
 * 已有后端虚拟环境时直接用它的解释器，省去在全新 uv 缓存里重新同步依赖。
 */
async function launchDesktop(): Promise<{ app: ElectronApplication; page: Page; home: string }> {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-desktop-e2e-"));
  const app = await electron.launch({
    args: [desktopRoot],
    env: {
      ...process.env,
      CODING_AGENT_DATA_DIR: home,
      ...(fs.existsSync(backendPython) ? { COSIR_BACKEND_PYTHON: backendPython } : {}),
    } as Record<string, string>,
  });
  const page = await app.firstWindow();
  return { app, page, home };
}

async function backendBaseUrl(page: Page): Promise<string> {
  const snapshot = await page.evaluate(() => window.cosirHost!.backend.snapshot());
  expect(snapshot.status).toEqual({ state: "ready" });
  return snapshot.backendBaseUrl!;
}

test("starts the host, reaches the workspace shell and isolates its data under the given home", async () => {
  const { app, page, home } = await launchDesktop();
  try {
    await expect(page).toHaveTitle("Cosir");
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
    await expect(page.getByText("无法连接本机后端")).toHaveCount(0);
    expect(await backendBaseUrl(page)).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
    expect(fs.existsSync(path.join(home, ".cosir", "storage"))).toBe(true);
  } finally {
    await app.close();
  }
});

test("closing the main window only hides it; the backend keeps running", async () => {
  const { app, page } = await launchDesktop();
  try {
    await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
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
  await expect(page.getByRole("navigation", { name: "工作区任务列表" })).toBeVisible({ timeout: 120_000 });
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
