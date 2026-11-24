import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";

test.beforeEach(async ({ request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
});

test("系统配置入口固定在左侧栏底部", async ({ page }) => {
  await page.goto("/");

  const sidebar = page.locator("aside");
  const settingsButton = page.getByRole("button", { name: "系统配置" });
  await expect(settingsButton).toBeVisible();

  const sidebarBox = await sidebar.boundingBox();
  const settingsBox = await settingsButton.boundingBox();
  expect(sidebarBox).not.toBeNull();
  expect(settingsBox).not.toBeNull();
  expect(settingsBox!.y).toBeGreaterThan(sidebarBox!.y + sidebarBox!.height - 140);
});

test("系统配置中心从 configuration 路由打开并支持配置编辑", async ({ page }) => {
  const reviewer = {
    agent_id: "reviewer",
    role: "child",
    description: "审查代码质量",
    system_prompt: "",
    allowed_tool_groups: ["文件"],
    max_steps: 100,
    model_config_id: null,
    model_settings: {},
    source: "user_file",
    path: ".cosir/agents/reviewer.json",
    deletable: true,
    validation_status: "valid",
    validation_error: null,
    file_name: "reviewer.json",
  };
  // 列表读集合 URL；保存/新建走条项 URL（PUT/POST /configuration/agents/{id}），必须返回单个对象。
  // 只认后端地址：页面源码模块（如 /components/configuration/agents/*.tsx）的路径也会命中同样的后缀。
  await page.route((url) => url.origin === API_ORIGIN && /^\/configuration\/agents(\/[^/]+)?$/.test(url.pathname), (route) => (
    route.request().method() === "GET"
      ? route.fulfill({ json: [reviewer] })
      : route.fulfill({ json: reviewer })
  ));
  await page.route(`${API_ORIGIN}/configuration/global-instructions`, (route) => route.fulfill({
    json: {
      content: "# 本机约束",
      path: ".cosir/AGENTS.md",
      token_length: 4,
      max_tokens: 32768,
      effective_on: "next_run",
    },
  }));
  await page.route(`${API_ORIGIN}/configuration/main-agent-prompt`, (route) => route.fulfill({
    json: {
      content: "# 主 Agent 协议",
      path: ".cosir/main_agent_system_prompt.md",
      token_length: 5,
      max_tokens: 2000,
      source: "user_file",
      effective_on: "next_run",
    },
  }));
  await page.route(`${API_ORIGIN}/configuration/environment`, (route) => route.fulfill({
    json: {
      groups: [{
        id: "web",
        label: "网页工具",
        description: "配置网页搜索与网页抓取。",
        fields: [
          {
            name: "FIRECRAWL_API_KEY",
            type: "string",
            component: "password",
            label: "API Key",
            secret: true,
            default: null,
            value: null,
            disk_value: null,
            configured: true,
            masked: true,
            source: "file",
            process_override: false,
            options: [],
            placeholder: null,
            clearable: true,
          },
        ],
      }],
    },
  }));

  await page.goto("/configuration");

  // /configuration 重定向到导航的第一个分类（不绑定是哪一个，顺序归 configuration-tabs）；页面标题由统一的页面头部面包屑承担。
  const settingsNav = page.getByRole("navigation", { name: "系统配置分类" });
  await expect(settingsNav.getByRole("link").first()).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("navigation", { name: "当前位置" })).toContainText("系统配置");
  await settingsNav.getByRole("link", { name: "子 Agent" }).click();
  await expect(page).toHaveURL(/\/configuration\/agents$/);
  await expect(page.getByText("集中管理子 Agent、全局指令与运行环境。敏感值只在本机配置文件中保存，不会进入对话或日志。", { exact: true })).toHaveCount(0);
  await expect(settingsNav.getByRole("link", { name: "子 Agent" })).toBeVisible();
  await expect(settingsNav.getByRole("link", { name: "全局 AGENTS.md" })).toBeVisible();
  await expect(settingsNav.getByRole("link", { name: "环境变量" })).toBeVisible();
  await expect(page.getByText("reviewer", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "编辑 reviewer" }).click();
  await expect(page.getByText("保存后从下一次 Run 开始生效，当前运行中的 Run 不会被改写。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "返回子 Agent 列表" })).toHaveText("返回子 Agent 列表");
  await expect(page.getByText("允许的工具组", { exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "文件" })).toBeChecked();
  // 交互终端仍不可授予；子 Agent 工具与其它可配置分组一样可以勾选。
  await expect(page.getByRole("checkbox", { name: "交互终端工具" })).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: "子Agent工具" })).toBeEnabled();
  await expect(page.getByRole("checkbox", { name: "文件" })).toBeEnabled();
  await expect(page.getByText("1 个工具", { exact: true })).toHaveCount(0);
  // 从对话进入配置时会话页保活在内容层之下，composer 的「选择模型」可能同样在 DOM 中，故限定到编辑器表单内。
  const editorModelSelector = page
    .locator("section")
    .filter({ hasText: "允许的工具组" })
    .getByRole("combobox", { name: "选择模型" });
  await expect(editorModelSelector).toHaveText("默认");
  await expect(page.getByText("模型参数", { exact: true })).toBeVisible();
  await expect(page.getByText("模型设置 JSON", { exact: true })).toHaveCount(0);
  await expect(page.getByText("未填写的参数不覆盖运行时默认值，保存时仍写入 model_settings JSON。", { exact: true })).toHaveCount(0);
  await expect(page.getByText("按工具组配置，保存时会展开为对应的工具名称。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: /推理强度/ })).toHaveValue("");
  await expect(page.getByRole("combobox", { name: "流式输出" })).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "响应格式" })).toHaveCount(0);
  await expect(page.getByText("高级模型参数", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "保存配置" }).click();
  // 保存结果走 toast（sonner 的 aria-live 区域），不再是内联 status Alert。
  await expect(page.getByText("配置更改成功", { exact: true })).toBeVisible();

  // 模型配置不再只能从 composer 下拉进入，配置中心有独立的「模型」标签。
  await settingsNav.getByRole("link", { name: "模型", exact: true }).click();
  await expect(page).toHaveURL(/\/configuration\/models$/);
  await expect(page.getByRole("region", { name: "连接 Demo" })).toBeVisible();

  await settingsNav.getByRole("link", { name: "全局 AGENTS.md" }).click();
  await expect(page).toHaveURL(/\/configuration\/instructions$/);
  await expect(page.getByText("保存后对新会话生效。", { exact: true })).toBeVisible();
  // 字节计数已移除，只保留 Token 估算。
  await expect(page.getByText("字节", { exact: true })).toHaveCount(0);
  await expect(page.getByText("估算 Token", { exact: true })).toBeVisible();
  await expect(page.getByText("下一次 Run 生效", { exact: true })).toHaveCount(0);

  await settingsNav.getByRole("link", { name: "主 Agent 提示词" }).click();
  await expect(page.getByRole("button", { name: "保存主 Agent 提示词" })).toBeVisible();
  await expect(page.getByText("保存后对新会话生效。", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Markdown 源码编辑器" })).toHaveText("# 主 Agent 协议");

  await settingsNav.getByRole("link", { name: "环境变量" }).click();
  await expect(page.getByRole("heading", { name: "网页工具" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重启后端" })).toHaveCount(0);
  await expect(page.getByText("当前进程", { exact: true })).toHaveCount(0);
  await expect(page.getByText("重启后预计（已保存文件）", { exact: true })).toHaveCount(0);
  await expect(page.getByText("默认值", { exact: true })).toHaveCount(0);
  await expect(page.getByText("输入新值将覆盖当前密钥；留空表示不变，点击清除按钮可删除已保存值。", { exact: true })).toHaveCount(0);

  // 清除已保存的密钥后显示「将清除」并可撤销，而不是继续提示「留空保持不变」。
  const apiKey = page.getByLabel("API Key", { exact: true });
  await expect(apiKey).toHaveAttribute("placeholder", "已配置（留空保持不变）");
  await page.getByRole("button", { name: "清除 API Key" }).click();
  await expect(page.getByText("将清除", { exact: true })).toBeVisible();
  await expect(apiKey).toHaveAttribute("placeholder", "保存后将清除已保存值");
  await page.getByRole("button", { name: "撤销清除 API Key" }).click();
  await expect(page.getByText("将清除", { exact: true })).toHaveCount(0);
  await expect(apiKey).toHaveAttribute("placeholder", "已配置（留空保持不变）");
  await expect(page.getByRole("button", { name: "保存变量" })).toBeDisabled();

  await page.getByLabel("API Key", { exact: true }).fill("new-secret");
  await page.getByRole("button", { name: /保存变量/ }).click();
  await expect(page.getByText("环境变量已保存", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "保存变量" })).toBeDisabled();
});

test("配置页各分类都不横向溢出：吸顶条铺满留白、贴边内容不冒出横向滚动条", async ({ page }) => {
  // 环境变量页带吸顶的保存条（-mx-2 铺满内容区留白），是最容易撑出横向滚动的一页。
  await page.route(`${API_ORIGIN}/configuration/environment`, (route) => route.fulfill({
    json: {
      groups: [{
        id: "browser",
        label: "浏览器",
        description: "限制内置浏览器同时打开的标签页数量。",
        fields: [{
          name: "BROWSER_MAX_TABS",
          type: "integer",
          component: "number",
          label: "全部会话的标签上限",
          secret: false,
          default: "20",
          value: "20",
          disk_value: null,
          configured: false,
          masked: false,
          source: "default",
          process_override: false,
          options: [],
          placeholder: null,
          clearable: false,
        }],
      }],
    },
  }));
  await page.setViewportSize({ width: 1280, height: 760 });

  for (const tab of ["appearance", "agents", "models", "main-agent", "instructions", "environment", "mcp", "skills"]) {
    await page.goto(`/configuration/${tab}`);
    await expect(page.getByRole("navigation", { name: "系统配置分类" }).locator('[aria-current="page"]')).toHaveAttribute("href", `/configuration/${tab}`);
    const overflow = await page.locator(".scroll-fade-top").evaluate((fade) => {
      const scroller = fade.parentElement!;
      return scroller.scrollWidth - scroller.clientWidth;
    });
    expect(overflow, `/configuration/${tab} 的内容区横向溢出`).toBe(0);
  }
});

test("子 Agent 保存期间的新编辑不随成功回执丢失：编辑器留着并仍算未保存", async ({ page }) => {
  let agent: Record<string, unknown> = {
    agent_id: "reviewer", role: "review", description: "original", system_prompt: "simple", allowed_tool_groups: [], max_steps: 100,
    model_config_id: null, model_settings: {}, source: "user_file", path: ".cosir/agents/reviewer.json", deletable: true,
    validation_status: "valid", validation_error: null, file_name: "reviewer.json",
  };
  let release!: () => void;
  const gate = new Promise<void>((resolve) => (release = resolve));
  const submitted: string[] = [];
  await page.route((url) => url.origin === API_ORIGIN && /^\/configuration\/agents(\/[^/]+)?$/.test(url.pathname), async (route) => {
    if (route.request().method() === "GET") return route.fulfill({ json: [agent] });
    const body = route.request().postDataJSON();
    submitted.push(body.description);
    await gate;
    agent = { ...agent, ...body };
    await route.fulfill({ json: agent });
  });
  await page.goto("/configuration/agents");
  await page.getByRole("button", { name: "编辑 reviewer" }).click();
  const description = page.getByPlaceholder("向主 Agent 说明这个子 Agent 负责什么");
  await description.fill("first");
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect.poll(() => submitted).toEqual(["first"]);
  await description.fill("second");
  release();
  await expect(page.getByText("已保存，之后的修改尚未保存", { exact: true })).toBeVisible();
  await expect(description).toHaveValue("second");
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect.poll(() => submitted).toEqual(["first", "second"]);
  await expect(description).toHaveCount(0);
});
