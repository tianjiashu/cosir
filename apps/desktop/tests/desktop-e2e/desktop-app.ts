import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { _electron as electron, expect, type ElectronApplication, type Page } from "@playwright/test";

const desktopRoot = path.resolve(__dirname, "..", "..");
const repositoryRoot = path.resolve(desktopRoot, "..", "..");
const backendPython = path.join(repositoryRoot, "apps", "backend", process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const capabilities = new Map<string, string>();

/** 测试驱动作为可信用户客户端；凭据从真实宿主出站请求捕获，只保留在测试内存。 */
export function desktopFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const token = capabilities.get(new URL(input).origin);
  const headers = new Headers(init.headers);
  if (token) headers.set("X-Cosir-Capability", token);
  return fetch(input, { ...init, headers });
}

/**
 * 启动真实宿主。数据根指向临时目录（`CODING_AGENT_DATA_DIR`）：SQLite、日志与运行期文件全部隔离，不碰开发者的真实数据。
 * 用户主目录也指向它（`HOME` / `USERPROFILE`）：系统级 Skill 目录固定在主目录的 `.agents/skills`、不随数据根走，
 * 不改主目录的话测试会读写开发者真实的 Skill。
 * 已有后端虚拟环境时直接用它的解释器，省去在全新 uv 缓存里重新同步依赖。
 */
export async function launchDesktop(): Promise<{ app: ElectronApplication; page: Page; home: string }> {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-desktop-e2e-"));
  const app = await electron.launch({
    args: [desktopRoot],
    env: {
      ...process.env,
      CODING_AGENT_DATA_DIR: home,
      HOME: home,
      USERPROFILE: home,
      ...(fs.existsSync(backendPython) ? { COSIR_BACKEND_PYTHON: backendPython } : {}),
    } as Record<string, string>,
  });
  const page = await app.firstWindow();
  return { app, page, home };
}

/**
 * 等应用外壳出现（启动门之后、后端可用）。认页头的「当前位置」导航而不是侧栏：屏幕窄于 1024 时侧栏收成抽屉
 * （`app-layout.tsx` 的 NARROW_VIEWPORT_QUERY），CI 跑机的屏幕常常就这么小，侧栏不在不代表宿主没起来。
 */
export async function waitForAppShell(page: Page, timeout = 120_000): Promise<void> {
  await expect(page.getByRole("navigation", { name: "当前位置" })).toBeVisible({ timeout });
}

export async function backendBaseUrl(page: Page): Promise<string> {
  const snapshot = await page.evaluate(() => window.cosirHost!.backend.snapshot());
  expect(snapshot.status).toEqual({ state: "ready" });
  const baseUrl = snapshot.backendBaseUrl!;
  if (!capabilities.has(new URL(baseUrl).origin)) await captureCapability(page, baseUrl);
  return baseUrl;
}

/**
 * 重新捕获某一代后端注入的身份。宿主重启后端口可能被复用，缓存不能沿用：
 * 验证「换代换证」时必须为新一代重新取一次。
 */
export async function captureCapability(page: Page, baseUrl: string): Promise<string> {
  // Playwright 能看到宿主注入后的真实请求头；不在产品接口中开放取 token 的入口。
  const probe = `${baseUrl}/health?desktop-test=${Date.now()}`;
  const observed = page.waitForRequest((request) => request.url() === probe);
  await page.evaluate((url) => fetch(url).then((r) => r.text()), probe);
  const headers = await (await observed).allHeaders();
  const token = headers["x-cosir-capability"] ?? "";
  expect(token).toBeTruthy();
  capabilities.set(new URL(baseUrl).origin, token);
  return token;
}

/** 测试内存里该代后端的凭据；没捕获过返回 undefined。 */
export function capabilityFor(baseUrl: string): string | undefined {
  return capabilities.get(new URL(baseUrl).origin);
}

/** 取或建工作区未开始的会话。首个 Run 建立前反复调用得到的是同一个，要第二个会话须先让它开始。 */
export async function unstartedSession(base: string, workspaceId: number): Promise<number> {
  const response = await desktopFetch(`${base}/workspaces/${workspaceId}/unstarted-session`, { method: "PUT" });
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
