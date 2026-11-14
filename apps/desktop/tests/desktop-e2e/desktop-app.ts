import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { _electron as electron, expect, type ElectronApplication, type Page } from "@playwright/test";

const desktopRoot = path.resolve(__dirname, "..", "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const backendPython = path.join(repositoryRoot, "apps", "backend", process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");

/**
 * 启动真实宿主。数据根指向临时目录（`CODING_AGENT_DATA_DIR`）：SQLite、日志与运行期文件全部隔离，不碰开发者的真实数据。
 * 已有后端虚拟环境时直接用它的解释器，省去在全新 uv 缓存里重新同步依赖。
 */
export async function launchDesktop(): Promise<{ app: ElectronApplication; page: Page; home: string }> {
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

export async function backendBaseUrl(page: Page): Promise<string> {
  const snapshot = await page.evaluate(() => window.cosirHost!.backend.snapshot());
  expect(snapshot.status).toEqual({ state: "ready" });
  return snapshot.backendBaseUrl!;
}

/** 取或建工作区未开始的会话。首个 Run 建立前反复调用得到的是同一个，要第二个会话须先让它开始。 */
export async function unstartedSession(base: string, workspaceId: number): Promise<number> {
  const response = await fetch(`${base}/workspaces/${workspaceId}/unstarted-session`, { method: "PUT" });
  expect(response.status, await response.clone().text()).toBe(200);
  return ((await response.json()) as { session_id: number }).session_id;
}

/** 像用户一样进新会话页并选中工作区：它未开始的会话不在侧栏，页面随即转到这个会话。 */
export async function openNewConversation(page: Page, workspaceName: string, sessionId: number): Promise<void> {
  await page.getByRole("button", { name: "新会话" }).first().click();
  await page.getByRole("button", { name: "选择工作区" }).click();
  await page.getByRole("option", { name: workspaceName }).click();
  await expect(page).toHaveURL(new RegExp(`/sessions/${sessionId}$`));
}
