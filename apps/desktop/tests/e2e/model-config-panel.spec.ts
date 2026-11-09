import { expect, test, type Page } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";

type Model = {
  config_id: string;
  connection_id: string;
  model_name: string;
  context_window_k: number;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  supports_image: boolean;
};
type Connection = { connection_id: string; name: string; base_url: string; api_key: string; models: Model[] };

const ROOT = "/configuration/model-connections";
const FILE_PATH = "/home/test/.cosir/config/models.json";

/** 有状态的模型配置后端：按真实接口形状增删改，记录每次写入与测试请求供断言。 */
function mockModelBackend(page: Page, initial: Connection[], { failing = new Set<string>(), discovered = [] as string[] } = {}) {
  const connections = structuredClone(initial);
  const calls = { tests: [] as Record<string, unknown>[], writes: [] as { method: string; path: string; body: unknown }[] };
  const toModel = (connectionId: string, fields: Pick<Model, "model_name" | "context_window_k"> & Partial<Model>): Model => ({
    supports_thinking: false,
    supports_reasoning_effort: false,
    supports_image: false,
    ...fields,
    config_id: `${connectionId}/${fields.model_name}`,
    connection_id: connectionId,
  });

  void page.route(`${API_ORIGIN}${ROOT}**`, async (route) => {
    const request = route.request();
    const path = decodeURIComponent(new URL(request.url()).pathname);
    const body = request.postDataJSON() as Record<string, unknown> | null;
    if (request.method() === "GET") return route.fulfill({ json: { path: FILE_PATH, connections } });
    if (path === `${ROOT}/discover`) return route.fulfill({ json: { models: discovered } });
    if (path === `${ROOT}/test`) {
      calls.tests.push(body!);
      const ok = !failing.has(String(body!.model_name));
      return route.fulfill({ json: ok ? { success: true, elapsed_ms: 8 } : { success: false, elapsed_ms: 5, error_message: "模型不存在" } });
    }
    calls.writes.push({ method: request.method(), path, body });
    if (path === ROOT) {
      const { models, ...fields } = body as Omit<Connection, "connection_id"> & { models: Model[] };
      const id = String(fields.name).toLowerCase();
      const created = { connection_id: id, ...fields, models: models.map((model) => toModel(id, model)) };
      connections.push(created);
      return route.fulfill({ json: created });
    }
    const [connectionId, , ...modelName] = path.slice(ROOT.length + 1).split("/");
    const connection = connections.find((item) => item.connection_id === connectionId)!;
    if (modelName.length > 0) {
      const model = connection.models.find((item) => item.model_name === modelName.join("/"))!;
      Object.assign(model, body);
      return route.fulfill({ json: model });
    }
    if (path.endsWith("/models")) {
      connection.models.push(...(body as { models: Model[] }).models.map((model) => toModel(connectionId!, model)));
    } else {
      Object.assign(connection, body);
    }
    return route.fulfill({ json: connection });
  });
  return { calls, failing };
}

const demo: Connection = {
  connection_id: "demo",
  name: "Demo",
  base_url: "https://api.example.com/v1",
  api_key: "test-api-key",
  models: [{ config_id: "demo/demo-model", connection_id: "demo", model_name: "demo-model", context_window_k: 128, supports_thinking: false, supports_reasoning_effort: false, supports_image: false }],
};

test("新建连接：Shift 连选多个模型，测试并保存只存通过的，失败的留在编辑卡里改完再提交", async ({ page }, testInfo) => {
  const { calls, failing } = mockModelBackend(page, [], {
    failing: new Set(["qwen-bad"]),
    discovered: ["qwen-a", "qwen-b", "qwen-c", "qwen-d", "qwen-bad"],
  });

  await page.goto("/settings/models");
  await expect(page.getByText("还没有模型连接")).toBeVisible();
  await page.getByRole("button", { name: "添加连接" }).click();
  const editor = page.getByRole("region", { name: "添加连接" });
  await editor.getByLabel("连接名称").fill("Packy");
  await editor.getByLabel("API Base URL").fill("https://packy.example/v1");
  await editor.getByLabel("API Key", { exact: true }).fill("sk-packy");

  const picker = editor.getByRole("group", { name: "可选模型" });
  await picker.getByRole("checkbox", { name: "qwen-a" }).click();
  await picker.getByRole("checkbox", { name: "qwen-c" }).click({ modifiers: ["Shift"] });
  await picker.getByRole("checkbox", { name: "qwen-bad" }).click();
  await expect(picker.getByRole("checkbox", { checked: true })).toHaveCount(4);
  await expect(editor.getByText("已选 4 个模型")).toBeVisible();

  // 统一设置把能力应用到全部已选模型。
  await editor.getByRole("button", { name: "统一设置" }).click();
  await editor.getByLabel("统一设置 图片输入").check();
  await editor.getByRole("button", { name: "应用到全部 4 个" }).click();
  await expect(editor.getByLabel("qwen-b 图片输入")).toBeChecked();
  await page.screenshot({ path: testInfo.outputPath("model-picker.png"), fullPage: true });

  await editor.getByRole("button", { name: "测试并保存 4 个模型" }).click();
  await expect(editor.getByText("模型不存在")).toBeVisible();
  expect(calls.tests.map((body) => body.api_key)).toEqual(["sk-packy", "sk-packy", "sk-packy", "sk-packy"]);
  expect(calls.writes).toEqual([{
    method: "POST",
    path: ROOT,
    body: expect.objectContaining({
      name: "Packy",
      models: ["qwen-a", "qwen-b", "qwen-c"].map((model_name) => expect.objectContaining({ model_name, supports_image: true })),
    }),
  }]);
  const card = page.getByRole("region", { name: "连接 Packy" });
  await expect(card.getByText("3 个模型")).toBeVisible();
  await expect(editor.getByLabel("连接名称")).toBeDisabled();
  await expect(picker.getByRole("checkbox", { name: /qwen-a/ })).toBeDisabled();

  // 失败的模型留在卡里：改好后再提交，追加到刚建出的连接下。
  failing.clear();
  await editor.getByRole("button", { name: "测试并保存 1 个模型" }).click();
  await expect(editor).toHaveCount(0);
  expect(calls.writes.at(-1)).toMatchObject({ method: "POST", path: `${ROOT}/packy/models` });
  await expect(card.getByText("4 个模型")).toBeVisible();
});

test("已有连接：添加模型时标出已添加，改 Key 先测试再保存，模型能力在行内编辑", async ({ page }, testInfo) => {
  const { calls } = mockModelBackend(page, [demo], { discovered: ["demo-model", "other-model"] });

  await page.goto("/settings/models");
  await expect(page.getByText(FILE_PATH)).toBeVisible();
  const card = page.getByRole("region", { name: "连接 Demo" });
  await card.getByRole("button", { name: "添加模型" }).click();
  const editor = page.getByRole("region", { name: "给「Demo」添加模型" });
  await expect(editor.getByRole("checkbox", { name: /demo-model/ })).toBeDisabled();
  await editor.getByRole("checkbox", { name: "other-model" }).click();
  await editor.getByRole("button", { name: "测试并保存 1 个模型" }).click();
  await expect(card.getByText("2 个模型")).toBeVisible();
  expect(calls.writes.at(-1)).toMatchObject({ method: "POST", path: `${ROOT}/demo/models` });

  await card.getByRole("button", { name: "编辑连接 Demo" }).click();
  const apiKeyField = card.locator("label").filter({ hasText: "API Key" });
  await expect(apiKeyField.locator('input[type="password"]')).toHaveValue("test-api-key");
  await apiKeyField.getByRole("button", { name: "显示 API Key" }).click();
  await apiKeyField.locator('input[type="text"]').fill("rotated-key");
  await expect(card.getByText("保存前会用「demo-model」测试一次")).toBeVisible();
  await card.getByRole("button", { name: "测试并保存" }).click();
  await expect(page.getByText("连接已保存", { exact: true })).toBeVisible();
  expect(calls.tests.at(-1)).toMatchObject({ api_key: "rotated-key", model_name: "demo-model" });
  expect(calls.writes.at(-1)).toEqual({ method: "PUT", path: `${ROOT}/demo`, body: { api_key: "rotated-key" } });

  await card.getByRole("button", { name: "编辑模型 demo-model" }).click();
  await card.getByLabel("demo-model 图片输入").check();
  await card.getByRole("button", { name: "测试并保存" }).click();
  await expect(card.getByText("128K · 图片输入")).toBeVisible();
  expect(calls.writes.at(-1)).toMatchObject({ method: "PUT", path: `${ROOT}/demo/models/demo-model`, body: { supports_image: true } });
  await card.getByText("other-model").hover();
  await page.screenshot({ path: testInfo.outputPath("model-connections.png"), fullPage: true });
});

test("输入框的模型选择器按连接分组", async ({ page }) => {
  mockModelBackend(page, [demo, { ...demo, connection_id: "packy", name: "Packy", models: [{ ...demo.models[0]!, config_id: "packy/qwen-a", connection_id: "packy", model_name: "qwen-a" }] }]);

  await page.goto("/");
  await page.getByRole("button", { name: "选择工作区" }).click();
  await page.getByRole("option", { name: /demo/ }).click();
  await page.getByRole("combobox", { name: "选择模型" }).click();
  const list = page.getByRole("listbox");
  await expect(list.getByRole("group", { name: "Demo" }).getByRole("option", { name: "demo-model" })).toBeVisible();
  await expect(list.getByRole("group", { name: "Packy" }).getByRole("option", { name: "qwen-a" })).toBeVisible();
});
