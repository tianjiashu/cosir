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
  await page.goto(`/workspaces/7/configuration/skills`);

  // 摘要直接给出 Agent 能用几个、目录多大。
  await expect(page.getByText("这个工作区里 Agent 可用 3 个")).toBeVisible();
  await expect(page.getByText("1,234")).toBeVisible();

  // 本工作区两节（空的专属一节也在，告诉用户往哪放），节标题自带作用域；继承来的系统级空专属目录不列。
  await expect(page.getByText(".cosir/skills", { exact: true })).toBeVisible();
  await expect(page.getByText("还没有。用右上角「导入」放进来。")).toBeVisible();
  await expect(page.getByText(".agents/skills", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "本工作区 · cosir 专属" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "系统级 · 共享" })).toBeVisible();
  await expect(page.getByText("· 只读")).toBeVisible();
  await expect(page.getByText("~/.agents/skills", { exact: true })).toBeVisible();
  await expect(page.getByText("~/.cosir/skills", { exact: true })).toHaveCount(0);
  // 被覆盖的系统级同名 Skill 只留短标签、悬停说明被谁覆盖，不给开关；无法加载的目录只计数一次。
  const shadowed = page.getByRole("link", { name: "已被覆盖" });
  await expect(shadowed).toBeVisible();
  await shadowed.hover();
  await expect(page.getByText("被「本工作区 · 共享」的同名 Skill 覆盖，点击查看生效的那份")).toBeVisible();
  await expect(page.getByRole("switch", { name: "停用 Skill review" })).toHaveCount(1);
  await expect(page.getByText("1 个无法加载")).toHaveCount(1);

  // 导入菜单只列来源；落点与「谁能看到」由 cosir 专属分组标题交代，菜单不重复。
  await page.getByRole("button", { name: "导入" }).click();
  await expect(page.getByText(/Codex 等看不到/)).toHaveCount(0);
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
  await page.goto(`/workspaces/7/configuration/skills`);

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

test("Skill 详情：文件夹默认收起、可展开，正文里的相对链接与行内代码路径打开 Skill 自己的文件", async ({ page }) => {
  const detail = {
    ...skill("report-kit", { scope: "system", kind: "cosir" }),
    content: "# 复盘\n\n写法见 [写法说明](references/style.md)，运行 `scripts/build.py`，不存在的 [旧文件](references/gone.md)。\n",
    also_at: [],
    directory: "/home/me/.cosir/skills/report-kit",
    metadata: {},
    files: [
      { path: "SKILL.md", size: 120, binary: false },
      { path: "references/style.md", size: 30, binary: false },
      { path: "scripts/build.py", size: 9, binary: false },
      { path: "scripts/helpers/fmt.py", size: 4, binary: false },
    ],
    files_truncated: false,
  };
  await page.route(`${API_ORIGIN}/configuration/skills/report-kit`, (route) => route.fulfill({ json: detail }));
  await page.route(new RegExp(`${API_ORIGIN}/configuration/skills/report-kit/file\\?`), (route) => {
    const path = new URL(route.request().url()).searchParams.get("path");
    return route.fulfill({ json: { path, content: path === "references/style.md" ? "# 写法\n\n短句。\n" : "print('build')\n" } });
  });
  await page.goto("/configuration/skills/report-kit");
  // 作用域、位置与「谁能看到」只在列表的分组标题里交代，详情页头不重复；没有其他副本时也不提「同一份」。
  await expect(page.getByRole("heading", { name: "report-kit" })).toBeVisible();
  await expect(page.getByText("只有 cosir 使用")).toHaveCount(0);
  await expect(page.getByText(/同一份也在/)).toHaveCount(0);

  const files = page.getByRole("navigation", { name: "Skill 文件" });
  // 默认收起：只看到文件夹与文件数，里面的文件不显示。
  const scripts = files.getByRole("button", { name: /scripts/ });
  await expect(scripts).toHaveAttribute("aria-expanded", "false");
  await expect(files.getByText("build.py")).toHaveCount(0);
  await scripts.click();
  await expect(files.getByText("build.py")).toBeVisible();
  await expect(files.getByRole("button", { name: /helpers/ })).toHaveAttribute("aria-expanded", "false");

  // 正文里的相对链接：在阅读区打开、树里展开到它；不存在的文件是纯文本。
  const reader = page.getByRole("region", { name: "阅读 SKILL.md" });
  await expect(reader.getByRole("link", { name: "旧文件" })).toHaveCount(0);
  await reader.getByRole("link", { name: "写法说明" }).click();
  await expect(page.getByRole("region", { name: "阅读 references/style.md" }).getByText("短句。")).toBeVisible();
  await expect(files.getByRole("button", { name: /references/ })).toHaveAttribute("aria-expanded", "true");
  await expect(files.getByRole("button", { name: /style\.md/ })).toHaveAttribute("aria-current", "true");

  // 行内代码里的路径同样打开对应文件。
  await files.getByRole("button", { name: /SKILL\.md/ }).click();
  await page.getByRole("region", { name: "阅读 SKILL.md" }).getByRole("button", { name: "scripts/build.py" }).click();
  await expect(page.getByRole("region", { name: "阅读 scripts/build.py" })).toBeVisible();
});
