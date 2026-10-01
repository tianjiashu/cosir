import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("http://127.0.0.1:8000/__test__/reset");
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

test("系统配置中心从 settings 路由打开并展示三类配置", async ({ page }) => {
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
    editable: true,
    deletable: true,
    validation_status: "valid",
    validation_error: null,
    file_name: "reviewer.json",
  };
  // 列表读集合 URL；保存/新建走条项 URL（PUT/POST /configuration/agents/{id}），必须返回单个对象。
  await page.route(/\/configuration\/agents(\/[^/]+)?$/, (route) => (
    route.request().method() === "GET"
      ? route.fulfill({ json: [reviewer] })
      : route.fulfill({ json: reviewer })
  ));
  await page.route("http://127.0.0.1:8000/configuration/global-instructions", (route) => route.fulfill({
    json: {
      content: "# 本机约束",
      path: ".cosir/AGENTS.md",
      token_length: 4,
      max_tokens: 32768,
      effective_on: "next_run",
    },
  }));
  await page.route("http://127.0.0.1:8000/configuration/main-agent-prompt", (route) => route.fulfill({
    json: {
      content: "# 主 Agent 协议",
      path: ".cosir/main_agent_system_prompt.md",
      token_length: 5,
      max_tokens: 2000,
      source: "user_file",
      effective_on: "next_run",
    },
  }));
  await page.route("http://127.0.0.1:8000/configuration/environment", (route) => route.fulfill({
    json: {
      groups: [{
        id: "langfuse",
        label: "Langfuse 可观测性",
        description: "配置 Agent 与工具调用链路追踪。",
        fields: [
          {
            name: "LANGFUSE_ENABLED",
            type: "boolean",
            component: "checkbox",
            label: "启用 Langfuse",
            secret: false,
            default: false,
            value: false,
            disk_value: false,
            configured: false,
            masked: false,
            source: "default",
            process_override: false,
            options: [],
            placeholder: null,
            clearable: true,
          },
          {
            name: "LANGFUSE_SECRET_KEY",
            type: "string",
            component: "password",
            label: "Secret Key",
            secret: true,
            default: null,
            value: null,
            disk_value: null,
            configured: false,
            masked: false,
            source: "default",
            process_override: false,
            options: [],
            placeholder: "请输入 Secret Key",
            clearable: true,
          },
        ],
      }],
    },
  }));

  await page.goto("/settings");

  await expect(page.getByText("系统配置中心", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "让本机 Agent 按你的方式工作" })).toBeVisible();
  await expect(page.getByText("集中管理子 Agent、全局指令与运行环境。敏感值只在本机配置文件中保存，不会进入对话或日志。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /子 Agent/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /全局指令/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /环境变量/ })).toBeVisible();
  await expect(page.getByText("reviewer", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "编辑 reviewer" }).click();
  await expect(page.getByText("保存后从下一次 Run 开始生效，当前运行中的 Run 不会被改写。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "返回 Agent 列表" })).toHaveText("返回 Agent 列表");
  await expect(page.getByText("允许的工具组", { exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "文件" })).toBeChecked();
  // 子 Agent 不可授予的分组保留展示但禁用，其它分组仍可勾选。
  await expect(page.getByRole("checkbox", { name: "交互终端工具" })).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: "子Agent工具" })).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: "文件" })).toBeEnabled();
  await expect(page.getByText("1 个工具", { exact: true })).toHaveCount(0);
  // 设置页是覆盖在 workspace 之上的浮层，composer 的「选择模型」同样在 DOM 中，故限定到编辑器表单内。
  const editorModelSelector = page
    .locator("section")
    .filter({ hasText: "允许的工具组" })
    .getByRole("combobox", { name: "选择模型" });
  await expect(editorModelSelector).toHaveText("默认");
  await expect(page.getByText("模型参数", { exact: true })).toBeVisible();
  await expect(page.getByText("模型设置 JSON", { exact: true })).toHaveCount(0);
  await expect(page.getByText("未填写的参数不覆盖运行时默认值，保存时仍写入 model_settings JSON。", { exact: true })).toHaveCount(0);
  await expect(page.getByText("按工具组配置，保存时会展开为对应的工具名称。", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "思考模式" })).toHaveValue("");
  await expect(page.getByRole("combobox", { name: "流式输出" })).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "响应格式" })).toHaveCount(0);
  await expect(page.getByText("高级模型参数", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect(page.getByRole("status")).toHaveText("配置更改成功");

  await page.getByRole("button", { name: /全局指令/ }).click();
  await expect(page.getByText("保存后实时生效，不影响前缀缓存。", { exact: true })).toBeVisible();
  // 字节计数已移除，只保留 Token 估算。
  await expect(page.getByText("字节", { exact: true })).toHaveCount(0);
  await expect(page.getByText("估算 Token", { exact: true })).toBeVisible();
  await expect(page.getByText("下一次 Run 生效", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: /主 Agent/ }).click();
  await expect(page.getByText("主 Agent 系统提示词", { exact: true })).toBeVisible();
  await expect(page.getByText("保存后实时生效，不影响前缀缓存。", { exact: true })).toBeVisible();
  await expect(page.locator("textarea").first()).toHaveValue("# 主 Agent 协议");

  await page.getByRole("button", { name: /环境变量/ }).click();
  await expect(page.getByRole("heading", { name: "Langfuse 可观测性" })).toBeVisible();
  await expect(page.getByText("Secret Key", { exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "启用 Langfuse" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重启后端" })).toHaveCount(0);
  await expect(page.getByText("当前进程", { exact: true })).toHaveCount(0);
  await expect(page.getByText("重启后预计（已保存文件）", { exact: true })).toHaveCount(0);
  await expect(page.getByText("默认值", { exact: true })).toHaveCount(0);
  await expect(page.getByPlaceholder("请输入 Secret Key", { exact: true })).toBeVisible();
  await expect(page.getByText("输入新值将覆盖当前密钥；留空表示不变，点击清除按钮可删除已保存值。", { exact: true })).toHaveCount(0);

  await page.getByPlaceholder("请输入 Secret Key", { exact: true }).fill("new-secret");
  await page.getByRole("button", { name: /保存变量/ }).click();
  await expect(page.getByRole("button", { name: "已保存" })).toBeVisible();
});
