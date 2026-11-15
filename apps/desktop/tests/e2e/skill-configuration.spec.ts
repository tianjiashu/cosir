import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { installFakeDesktopHost } from "./fake-desktop-host";

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

function skill(name: string, extra: Record<string, unknown> = {}) {
  return {
    name,
    description: `${name} 的说明`,
    enabled: true,
    scope: "workspace",
    kind: "shared",
    overrides: false,
    warnings: [],
    manual_only: false,
    has_scripts: false,
    standing: "effective",
    ...extra,
  };
}

// 后端按优先级从高到低给出：本工作区专属、本工作区共享、系统级专属（空）、系统级共享。
const catalog = {
  agent_skills: 3,
  agent_tokens: 1234,
  groups: [
    { scope: "workspace", kind: "cosir", location: ".cosir/skills", skipped: [], skills: [] },
    {
      scope: "workspace", kind: "shared", location: ".agents/skills",
      skipped: [{ directory: "broken", reason: "Skill description 不能为空" }],
      skills: [skill("review", { overrides: true, has_scripts: true })],
    },
    { scope: "system", kind: "cosir", location: "~/.cosir/skills", skipped: [], skills: [] },
    {
      scope: "system", kind: "shared", location: "~/.agents/skills", skipped: [],
      skills: [
        skill("lark-base", { scope: "system" }),
        skill("lark-mail", { scope: "system" }),
        skill("review", { scope: "system", standing: "shadowed" }),
      ],
    },
  ],
};

const rescanned = {
  ...catalog,
  groups: [...catalog.groups.slice(0, 3), { ...catalog.groups[3], skills: [...catalog.groups[3].skills, skill("fresh", { scope: "system" })] }],
};

test("工作区 Skills 页按身份分卡：摘要、覆盖关系、导入落点、重新扫描、搜索后批量停用", async ({ page }) => {
  const enabledRequests: unknown[] = [];
  // 用其他工具装好新 Skill 之前，目录读到的都是旧的。
  let installed = false;
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/skill-catalog`, (route) => route.fulfill({ json: installed ? rescanned : catalog }));
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/skills/enabled`, async (route) => {
    enabledRequests.push(route.request().postDataJSON());
    await route.fulfill({ json: {} });
  });
  await page.goto(`/workspaces/7/settings/skills`);

  // 摘要直接给出 Agent 能用几个、目录多大。
  await expect(page.getByText("在这个工作区里，Agent 能用 3 个 Skill")).toBeVisible();
  await expect(page.getByText("1,234")).toBeVisible();

  // 本工作区两张卡（空的专属卡也在，告诉用户往哪放）；继承来的系统级空专属目录不列。
  await expect(page.getByText(".cosir/skills", { exact: true })).toBeVisible();
  await expect(page.getByText("还没有。用右上角「导入」放进来，只有 cosir 使用。")).toBeVisible();
  await expect(page.getByText(".agents/skills", { exact: true })).toBeVisible();
  await expect(page.getByText("继承自系统级 · 在系统设置中管理")).toBeVisible();
  await expect(page.getByText("~/.agents/skills", { exact: true })).toBeVisible();
  await expect(page.getByText("~/.cosir/skills", { exact: true })).toHaveCount(0);
  // 被覆盖的系统级同名 Skill 写明被谁覆盖、不给开关；无法加载的目录只计数。
  await expect(page.getByRole("link", { name: "被「本工作区 · 共享」的同名 Skill 覆盖 →" })).toBeVisible();
  await expect(page.getByRole("switch", { name: "停用 Skill review" })).toHaveCount(1);
  await expect(page.getByText("1 个无法加载")).toBeVisible();

  // 导入菜单写明落在专属目录、其他工具看不到。
  await page.getByRole("button", { name: "导入" }).click();
  await expect(page.getByText("导入到 .cosir/skills · 只有 cosir 使用，Codex 等看不到")).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "ZIP 压缩包…" })).toBeVisible();
  await page.keyboard.press("Escape");

  // 重新扫描：重读目录并告诉用户变了什么。
  await expect(page.getByText("fresh", { exact: true })).toHaveCount(0);
  installed = true;
  await page.getByRole("button", { name: "重新扫描" }).click();
  await expect(page.getByText("扫描完成：新增 1 个，移除 0 个")).toBeVisible();
  await expect(page.getByText("fresh", { exact: true })).toBeVisible();

  // 搜索后只剩命中的条目，批量停用一次请求发出全部名称。
  await page.getByRole("textbox", { name: "搜索 Skill" }).fill("lark");
  await expect(page.getByText("review 的说明")).toHaveCount(0);
  // 继承来的系统级只读，批量操作只作用于本工作区：这里没有可停用的。
  await expect(page.getByRole("button", { name: /停用这/ })).toHaveCount(0);
  await page.getByRole("textbox", { name: "搜索 Skill" }).fill("review");
  await page.getByRole("button", { name: "停用这 1 个" }).click();
  await expect.poll(() => enabledRequests).toEqual([{ names: ["review"], enabled: false }]);
});

test("导入撞上专属目录里的同名 Skill：弹窗说明新旧两份，确认替换后带上选择再导入", async ({ page }) => {
  await installFakeDesktopHost(page, { directory: "/tmp/import-source/review" });
  const importRequests: unknown[] = [];
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/skill-catalog`, (route) => route.fulfill({ json: catalog }));
  await page.route(`${API_ORIGIN}/workspaces/7/configuration/skills/import`, async (route) => {
    const body = route.request().postDataJSON() as { resolution?: string };
    importRequests.push(body);
    if (body.resolution === "replace") {
      await route.fulfill({ status: 201, json: skill("review", { kind: "cosir" }) });
      return;
    }
    await route.fulfill({
      status: 409,
      json: {
        detail: {
          code: "skill_import_conflict",
          message: "cosir 专属目录里已有同名 Skill，内容不同：review",
          conflict: {
            name: "review",
            relation: "replace",
            description: "导入的新版本",
            existing: [{ scope: "workspace", kind: "cosir", location: "~/proj/.cosir/skills/review", description: "旧版本", same_content: false }],
          },
        },
      },
    });
  });
  await page.goto(`/workspaces/7/settings/skills`);

  await page.getByRole("button", { name: "导入" }).click();
  await page.getByRole("menuitem", { name: "Skill 文件夹…" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("替换「review」？")).toBeVisible();
  await expect(dialog.getByText(/现有：旧版本/)).toBeVisible();
  await expect(dialog.getByText(/导入：导入的新版本/)).toBeVisible();
  await dialog.getByRole("button", { name: "替换" }).click();

  await expect(page.getByText("已替换 review")).toBeVisible();
  await expect.poll(() => importRequests).toEqual([
    { source_path: "/tmp/import-source/review", source_type: "directory" },
    { source_path: "/tmp/import-source/review", source_type: "directory", resolution: "replace" },
  ]);
});
