import { expect, test } from "@playwright/test";

const changesPath = "http://127.0.0.1:8000/tasks/42/changes";
const netDiff = [
  "diff --git a/src/app.ts b/src/app.ts",
  "--- a/src/app.ts",
  "+++ b/src/app.ts",
  "@@ -1,1 +1,1 @@",
  "-baseline text",
  "+final text",
].join("\n");

const pendingChangeSet = {
  task_id: 42,
  files: [{
    change_id: "chg_app",
    paths: ["src/app.ts"],
    action: "modified",
    status: "pending",
    last_run_id: 77,
    operation_count: 3,
    net_diff: {
      state: "verified",
      additions: 1,
      deletions: 1,
      patch: netDiff,
      truncated: false,
      has_unrendered_changes: false,
    },
  }],
};

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
  await request.post("http://127.0.0.1:8000/__test__/seed-task", {
    data: { taskId: 42, title: "文件变更验收" },
  });
});

test("显示基线到最终状态的净 Diff，并将整个文件组回退到基线", async ({ page }) => {
  let revertBody: Record<string, unknown> | null = null;
  await page.route(changesPath, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: pendingChangeSet });
      return;
    }
    await route.fulfill({
      json: {
        task_id: 42,
        results: [{ change_id: "chg_app", outcome: "reverted" }],
        change_set: {
          task_id: 42,
          files: [],
        },
      },
    });
  });
  await page.route(`${changesPath}/revert`, async (route) => {
    revertBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      json: {
        task_id: 42,
        results: [{ change_id: "chg_app", outcome: "reverted" }],
        change_set: {
          task_id: 42,
          files: [],
        },
      },
    });
  });

  await page.goto("/tasks/42");
  const panel = page.getByRole("complementary", { name: "任务文件变更" });
  await expect(panel.getByText("1 个待处理文件组")).toBeVisible();
  await expect(panel.getByText("修改 · 3 次操作")).toBeVisible();
  await panel.getByRole("button", { name: "展开 src/app.ts 的最终净 Diff" }).click();
  await expect(panel.getByText("baseline text", { exact: true })).toBeVisible();
  await expect(panel.getByText("final text", { exact: true })).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await panel.getByRole("button", { name: "回退到基线", exact: true }).click();
  await expect.poll(() => revertBody).toEqual({ change_ids: ["chg_app"] });
  await expect(panel.getByText("0 个待处理文件组")).toBeVisible();
  await expect(panel.getByText("已回退到基线")).toBeVisible();
});
