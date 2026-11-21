import { expect, test, type APIRequestContext } from "@playwright/test";

import type { TransportMessagePart } from "../../lib/assistant/contract";
import { API_ORIGIN, REAL_BACKEND_ORIGIN } from "./e2e-env";
import { completedState, seedState } from "./transport-fixtures";
import { WorkspaceFilesBackend } from "./workspace-files-backend";

/**
 * 交付物卡片的现读与工作区级监听（真实后端）：面板关着时会话照样订阅工作区，文件变了胶囊跟着变，离开会话订阅归零。
 * 单独一个文件、单独一个夹具工作区：会改写、删除 src/main.py，不影响其他用例。
 */
const backend = new WorkspaceFilesBackend();
const SESSION_ID = 42;

test.beforeAll(async ({ request }) => {
  await backend.start(request);
});
test.afterAll(async () => {
  await backend.stop();
});

async function subscriptions(request: APIRequestContext): Promise<number> {
  return (await (await request.get(`${REAL_BACKEND_ORIGIN}/__test__/files/watch-status`)).json()).subscriptions;
}

test("面板关着时会话订阅工作区；文件更新后胶囊大小跟着变，删除后显示「已不存在」；离开会话订阅归零", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "交付物现读" } });
  const present: TransportMessagePart = {
    type: "tool-call", toolCallId: "call-present", toolName: "present_deliverables", presentation: "quiet",
    args: { items: [{ target: "src/main.py" }] }, status: "completed", error: null, awaitingUser: false,
    deliverables: [{ type: "file", key: "/w/src/main.py", caption: null, workspacePath: "src/main.py", absolutePath: "/w/src/main.py", kind: "text", size: 999, width: null, height: null }],
  };
  await seedState(request, SESSION_ID, completedState(1, { id: "u-1", parts: [{ type: "text", text: "交付", status: "completed" }] }, [
    present, { type: "text", text: "写好了。", status: "completed", finalReply: true },
  ]));
  await backend.route(page);
  await page.goto(`/sessions/${SESSION_ID}`);
  const capsule = page.locator('[data-slot="deliverable-capsule"]').filter({ hasText: "main.py" });
  // 交付时记的是 999 B；现读到的是夹具文件的真实大小。
  await expect(capsule).toContainText("23 B");
  await expect.poll(() => subscriptions(request)).toBe(1);

  await request.post(`${REAL_BACKEND_ORIGIN}/__test__/files/update`);
  await expect(capsule).toContainText("22 B", { timeout: 10_000 });
  await request.post(`${REAL_BACKEND_ORIGIN}/__test__/files/delete`);
  await expect(capsule).toContainText("已不存在", { timeout: 10_000 });
  await expect(capsule).toHaveAttribute("data-unavailable", "true");

  await page.goto("/");
  await expect.poll(() => subscriptions(request)).toBe(0);
});
