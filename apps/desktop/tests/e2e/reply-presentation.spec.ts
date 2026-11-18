import { expect, test, type APIRequestContext, type Page, type Route } from "@playwright/test";

import type { TransportDeliverable, TransportMessagePart, TransportState } from "../../lib/assistant/contract";
import { completedState, seedState } from "./transport-fixtures";
import { fakeBrowserCalls, installFakeDesktopHost } from "./fake-desktop-host";
import { API_ORIGIN as origin } from "./e2e-env";
import { stubWorkspaceFiles, type StubFile } from "./workspace-files-stubs";

/**
 * 回复呈现（`present_deliverables` / `offer_next_step`）：交付物是最终回复上方的一组胶囊（一次交付一张卡，可带说明），只读 part 上的交付记录；下一步建议只挂在
 * 会话最后一条正常完成的回复上，点按钮等于替用户发出标签原文。
 */

const SESSION_ID = 42;
const LAUNCH = { modelConfigId: "demo/demo-model", reasoningEffort: null };
const ROOT = "C:/demo";

const file = (workspacePath: string | null, absolutePath: string, caption: string | null = null, size = 1229) =>
  ({ type: "file", key: absolutePath, caption, workspacePath, absolutePath, kind: "text", size, width: null, height: null }) as const;
const DELIVERABLES = [
  file("docs/report.md", `${ROOT}/docs/report.md`, "结论在第 2 节"),
  { type: "directory", key: `${ROOT}/dist`, caption: null, workspacePath: "dist", absolutePath: `${ROOT}/dist` },
  { type: "url", key: "http://localhost:5173/", caption: "本地预览", url: "http://localhost:5173" },
  file(null, "/tmp/outside/chart.csv"),
  file(null, "/tmp/outside/page.html"),
] as const;

const FILES: TransportMessagePart = {
  type: "tool-call", toolCallId: "call-files", toolName: "present_deliverables", presentation: "quiet",
  args: { items: DELIVERABLES.map((item) => ({ target: item.key })) },
  intent: null, status: "completed", error: null, awaitingUser: false,
  deliverables: [...DELIVERABLES],
};
const OFFER: TransportMessagePart = {
  type: "tool-call", toolCallId: "call-offer", toolName: "offer_next_step", presentation: "quiet",
  args: { ask: "要把同样的修复应用到移动端吗？", replies: ["好，应用", "先不用"] },
  intent: null, status: "completed", error: null, awaitingUser: false,
  display_data: { kind: "next-step", ask: "要把同样的修复应用到移动端吗？", replies: ["好，应用", "先不用"] },
};

function deliveredState(): TransportState {
  const state = completedState(1, { id: "u-1", parts: [{ type: "text", text: "写一份报告", status: "completed" }] }, [
    FILES, OFFER, { type: "text", text: "报告写好了。", status: "completed", finalReply: true },
  ]);
  return { ...state, activity: { ...state.activity!, launch: LAUNCH } };
}

async function open(page: Page, request: APIRequestContext, state: TransportState) {
  await seedState(request, SESSION_ID, state);
  await page.goto(`/sessions/${SESSION_ID}`);
  await expect(page.getByText("报告写好了。")).toBeVisible();
}

async function sentTexts(request: APIRequestContext): Promise<string[]> {
  const telemetry = await (await request.get(`${origin}/__test__/telemetry`)).json() as {
    requestBodies: Array<{ message?: { parts?: Array<{ type: string; text?: string }> } }>;
  };
  return telemetry.requestBodies.flatMap((body) => body.message?.parts?.flatMap((part) => (part.type === "text" && part.text ? [part.text] : [])) ?? []);
}

test.beforeEach(async ({ page, request }) => {
  await stubWorkspaceFiles(page, { files: STUB_FILES, directories: DIRECTORIES });
  await page.route(`**/sessions/${SESSION_ID}/browser-context`, (route) => route.fulfill({ json: { limits: { per_session: 5, total: 20 }, download_dir: "/tmp/dl" } }));
  await request.post(`${origin}/__test__/reset`);
  await request.post(`${origin}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "回复呈现" } });
  await page.addInitScript(() => window.localStorage.clear());
});

/** 快照里的工作区内文件：卡片现读的信息与文件面板打开时用的同一份桩。 */
const STUB_FILES = { "docs/report.md": { size: 1229, kind: "markdown" as const } };
const DIRECTORIES = { "": ["dist", "docs"] };

const capsule = (page: Page, name: string) => page.locator('[data-slot="deliverable-capsule"]').filter({ hasText: name });

test("交付物是一组胶囊：名称、大小一格与提示条；两个工具都不在过程里留调用行", async ({ page, request }) => {
  await installFakeDesktopHost(page);
  await open(page, request, deliveredState());

  const card = page.locator('[data-slot="deliverables-card"]');
  await expect(card.locator('[data-slot="deliverable-capsule"]')).toHaveCount(5);
  await expect(capsule(page, "report.md")).toContainText("1.2 KB");
  await expect(capsule(page, "dist")).toContainText("文件夹");
  await expect(capsule(page, "本地预览")).toContainText("localhost:5173");
  await expect(page.locator('[data-slot="process-head"]')).toHaveCount(0);

  await capsule(page, "report.md").getByRole("button").first().hover();
  const tip = page.locator('[data-slot="tooltip-content"]');
  await expect(tip).toContainText("结论在第 2 节");
  await expect(tip).toContainText("docs/report.md");
  // 次按钮是出口菜单：文件与目录都有（工作区外的也有），网址没有。平时不可见，按 DOM 计数。
  await expect(capsule(page, "report.md").getByRole("button", { name: "更多操作 report.md" })).toBeVisible();
  await expect(capsule(page, "chart.csv").locator("button")).toHaveCount(2);
  await expect(capsule(page, "本地预览").locator("button")).toHaveCount(1);
});

test("点胶囊到对的地方：文件进面板、目录进文件树、工作区外网页进内置浏览器、其余在文件夹中显示", async ({ page, request }) => {
  await installFakeDesktopHost(page);
  await open(page, request, deliveredState());
  const files = page.getByTestId("workspace-files-surface");

  await capsule(page, "report.md").getByRole("button").first().click();
  await expect(files.getByRole("tab", { name: /report\.md/ })).toBeVisible();

  await capsule(page, "dist").getByRole("button").first().click();
  await expect(files.getByRole("treeitem", { name: /dist/ })).toHaveAttribute("aria-expanded", "true");

  await capsule(page, "chart.csv").getByRole("button").click();
  await capsule(page, "page.html").getByRole("button").click();
  await capsule(page, "report.md").hover();
  await capsule(page, "report.md").getByRole("button", { name: "更多操作 report.md" }).click();
  await page.getByRole("menuitem", { name: "在文件夹中显示" }).click();
  await expect.poll(async () => (await fakeBrowserCalls(page)).filter((call) => call.method !== "answerDialog")).toEqual([
    { method: "files.reveal", args: ["/tmp/outside/chart.csv"] },
    { method: "browser.open", args: [SESSION_ID, expect.anything()] },
    { method: "browser.navigate", args: ["fake-tab", { url: "file:///tmp/outside/page.html" }] },
    { method: "files.reveal", args: [`${ROOT}/docs/report.md`] },
  ]);
});

test("没有桌面宿主时没有次按钮，工作区外的非网页文件不可点", async ({ page, request }) => {
  await open(page, request, deliveredState());
  await expect(page.locator('[data-slot="deliverable-capsule"] button[aria-label^="更多操作"]')).toHaveCount(0);
  await expect(capsule(page, "chart.csv").getByRole("button")).toBeDisabled();
  await expect(capsule(page, "report.md").getByRole("button")).toBeEnabled();
});

test("点下一步建议的按钮就是替用户发出标签原文，发出后建议消失", async ({ page, request }) => {
  await open(page, request, deliveredState());

  const offer = page.locator('[data-slot="next-step-card"]');
  await expect(offer).toContainText("要把同样的修复应用到移动端吗？");
  await expect(offer.getByRole("button")).toHaveText(["好，应用", "先不用"]);

  await offer.getByRole("button", { name: "好，应用" }).click();
  await expect.poll(() => sentTexts(request)).toEqual(["好，应用"]);
  await expect(offer).toHaveCount(0);
});

test("建议只挂在最后一条回复上：之后又有回合时不再显示，交付物卡片仍在", async ({ page, request }) => {
  const first = deliveredState();
  const second = completedState(2, { id: "u-2", parts: [{ type: "text", text: "再看看别的", status: "completed" }] }, [
    { type: "text", text: "看过了。", status: "completed", finalReply: true },
  ]);
  // 结局属于 Run 最后一个回合的回复；这里两个回合各是一个已完成的 Run。
  await open(page, request, { ...second, messages: [...first.messages, ...second.messages] });

  await expect(page.getByText("看过了。")).toBeVisible();
  await expect(page.locator('[data-slot="deliverables-card"]')).toBeVisible();
  await expect(page.locator('[data-slot="next-step-card"]')).toHaveCount(0);
});

/** 只有交付物、没有建议的一个回合。 */
function deliverablesOnly(deliverables: TransportDeliverable[]): TransportState {
  const part: TransportMessagePart = { ...(FILES as Extract<TransportMessagePart, { type: "tool-call" }>), args: { items: deliverables.map((item) => ({ target: item.key })) }, deliverables };
  const state = completedState(1, { id: "u-1", parts: [{ type: "text", text: "交付", status: "completed" }] }, [
    part, { type: "text", text: "报告写好了。", status: "completed", finalReply: true },
  ]);
  return { ...state, activity: { ...state.activity!, launch: LAUNCH } };
}
/** 图片的交付记录带交付时的宽高（卡片排版只看它），读不出宽高时为空。 */
const image = (name: string, width: number | null = null, height: number | null = null) => ({ ...file(name, `${ROOT}/${name}`), kind: "image" as const, width, height });

test("胶囊现读：大小随文件变化，已不存在与无法访问置灰不可点，其他错误写「—」；会话维持一条监听", async ({ page, request }) => {
  await installFakeDesktopHost(page);
  const stub = await stubWorkspaceFiles(page, {
    files: { "docs/report.md": { size: 2048, kind: "markdown" }, "gone.md": "missing", "secret.md": "denied" },
  });
  await page.route(/\/files\/info\?.*broken\.md/, (route) => route.fulfill({ status: 500, json: { detail: "读取出错" } }));
  await open(page, request, deliverablesOnly([
    file("docs/report.md", `${ROOT}/docs/report.md`), file("gone.md", `${ROOT}/gone.md`),
    file("secret.md", `${ROOT}/secret.md`), file("broken.md", `${ROOT}/broken.md`),
  ]));
  await expect(capsule(page, "report.md")).toContainText("2 KB");
  for (const [name, text] of [["gone.md", "已不存在"], ["secret.md", "无法访问"]] as const) {
    await expect(capsule(page, name)).toContainText(text);
    await expect(capsule(page, name)).toHaveAttribute("data-unavailable", "true");
    await expect(capsule(page, name).getByRole("button")).toHaveCount(1);
    await expect(capsule(page, name).getByRole("button")).toBeDisabled();
  }
  await expect(capsule(page, "broken.md")).toContainText("—");
  await expect(capsule(page, "broken.md").getByRole("button").first()).toBeEnabled();
  await expect.poll(stub.watchCount).toBe(1);
});

test("多张图片排成方块（缩略图），点开灯箱看原图；被删的图保持方块、换图标", async ({ page, request }) => {
  await stubWorkspaceFiles(page, {
    files: { "a.png": { size: 900, kind: "image", width: 800, height: 400 }, "b.png": { size: 900, kind: "image", width: 40, height: 40 }, "c.png": "missing" },
  });
  await open(page, request, deliverablesOnly([image("a.png", 800, 400), image("b.png", 40, 40), image("c.png"), file("notes.md", `${ROOT}/notes.md`)]));
  const tiles = page.locator('[data-slot="deliverables-card"] [data-slot="image-tile"]');
  await expect(tiles).toHaveCount(3);
  await expect(tiles.nth(0).locator("img")).toHaveAttribute("src", /preview=tile/);
  await expect(tiles.nth(2)).toHaveAttribute("data-unavailable", "true");
  // 胶囊另起一行，排在方块下面。
  await expect(capsule(page, "notes.md")).toBeVisible();
  await tiles.nth(0).getByRole("button").click();
  const lightbox = page.getByRole("dialog");
  await expect(lightbox.locator("img").first()).toHaveAttribute("src", /resource\?path=a\.png&version=v1$/);
});

test("只有一张图时是单张大图卡：按原图比例装进 360×240", async ({ page, request }) => {
  await stubWorkspaceFiles(page, { files: { "chart.png": { size: 900, kind: "image", width: 800, height: 400 }, "notes.md": { size: 10 } } });
  await open(page, request, deliverablesOnly([image("chart.png", 800, 400), file("notes.md", `${ROOT}/notes.md`)]));
  const card = page.locator('[data-slot="deliverable-image-card"]');
  await expect(card).toBeVisible();
  expect(await card.evaluate((element) => [element.clientWidth, element.clientHeight])).toEqual([358, 178]);
  await expect(card.locator("img")).toHaveAttribute("src", /preview=card/);
  await expect(capsule(page, "notes.md")).toBeVisible();
});

// 潜在缺陷：读到文件信息前先画方块、读到后换成卡片（高度跳）；卡片图加载失败时没有失败态，只剩空框。
test("单张大图卡读到文件信息前就占好位置，图加载失败时在同一个框里说明", async ({ page, request }) => {
  await stubWorkspaceFiles(page, { files: { "chart.png": { size: 900, kind: "image", width: 800, height: 400 } } });
  let releaseInfo = () => {};
  const infoHeld = new Promise<void>((resolve) => (releaseInfo = resolve));
  await page.route(/\/files\/info\?.*chart\.png/, async (route) => {
    await infoHeld;
    await route.fallback();
  });
  await page.route(/\/files\/resource\?.*chart\.png/, (route) => route.fulfill({ status: 500, body: "" }));
  await open(page, request, deliverablesOnly([image("chart.png", 800, 400)]));
  const card = page.locator('[data-slot="deliverable-image-card"]');
  await expect(card).toBeVisible();
  await expect(page.locator('[data-slot="deliverables-card"] [data-slot="image-tile"]')).toHaveCount(0);
  expect(await card.evaluate((element) => [element.clientWidth, element.clientHeight])).toEqual([358, 178]);
  releaseInfo();
  await expect(card).toHaveAttribute("data-unavailable", "true");
  await expect(card).toContainText("无法预览");
  expect(await card.evaluate((element) => [element.clientWidth, element.clientHeight])).toEqual([358, 178]);
});

/** 自己接管监听请求：`changed()` 回一条变化事件并结束这条连接，前端随即失效文件缓存并重连（等同于断线重连后的整体重读）。 */
async function controllableWatch(page: Page) {
  const routes: Route[] = [];
  await page.route(/\/files\/watch/, (route) => void routes.push(route));
  return {
    changed: async () => {
      for (const route of routes.splice(0)) await route.fulfill({ body: '{"type":"changed"}\n', contentType: "application/x-ndjson" });
    },
  };
}

// 回归：资源加载失败曾按「重读信息后版本没变」判为内容坏了，后端短暂不在一次，同一版本就永久「无法预览」。
test("后端暂时不在导致的加载失败不判为「无法预览」", async ({ page, request }) => {
  await stubWorkspaceFiles(page, { files: { "chart.png": { size: 900, kind: "image", width: 800, height: 400 } } });
  await page.route(/\/files\/resource\?/, (route) => route.abort("connectionrefused"));
  await open(page, request, deliverablesOnly([image("chart.png", 800, 400)]));
  const card = page.locator('[data-slot="deliverable-image-card"]');
  await expect(card.locator("img")).toHaveCount(1);
  await page.waitForTimeout(500);
  await expect(card).not.toHaveAttribute("data-unavailable");
});

// 回归：资源与文件信息同时失败时，查询保留旧数据，后端回来后仍判为「无法预览」，同一版本永不恢复。
test("后端重启回来（地址换新）后，大图卡照常显示", async ({ page, request }) => {
  await installFakeDesktopHost(page);
  await stubWorkspaceFiles(page, { files: { "chart.png": { size: 900, kind: "image", width: 800, height: 400 } } });
  let down = false;
  let infoCalls = 0;
  await page.route(/\/files\/info\?/, (route) => { infoCalls++; return down ? route.abort("connectionrefused") : route.fallback(); });
  await page.route(/\/files\/resource\?/, async (route) => { down = true; await route.abort("connectionrefused"); });
  await open(page, request, deliverablesOnly([image("chart.png", 800, 400)]));
  const card = page.locator('[data-slot="deliverable-image-card"]');
  await expect.poll(() => infoCalls).toBeGreaterThanOrEqual(1);
  await page.waitForTimeout(500);
  await expect(card).not.toHaveAttribute("data-unavailable");
  down = false;
  await page.unroute(/\/files\/resource\?/);
  // 同一个测试服务换一种写法，等同于重启后宿主给出的新地址。
  await page.evaluate((url) => (window as unknown as { fakeBackendRestart: (url: string) => void }).fakeBackendRestart(url), origin.replace("127.0.0.1", "localhost"));
  const img = card.locator("img");
  await expect(img).toHaveAttribute("src", /localhost/);
  await expect.poll(() => img.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  await expect(card).not.toHaveAttribute("data-unavailable");
});

// 回归：方块的 key 含资源地址，文件一变就重新挂载，键盘焦点掉回页面。
test("文件版本变化时方块原地换图，焦点留在方块上", async ({ page, request }) => {
  const files: Record<string, StubFile> = { "a.png": { size: 900, kind: "image", width: 800, height: 400 }, "b.png": { size: 900, kind: "image", width: 40, height: 40 } };
  await stubWorkspaceFiles(page, { files });
  const watch = await controllableWatch(page);
  await open(page, request, deliverablesOnly([image("a.png", 800, 400), image("b.png", 40, 40)]));
  const tile = page.locator('[data-slot="deliverables-card"] [data-slot="image-tile"]').first();
  await expect(tile.locator("img")).toHaveAttribute("src", /version=v1/);
  await tile.evaluate((element) => ((window as unknown as { tile: Element }).tile = element));
  await tile.getByRole("button").focus();
  files["a.png"] = { size: 900, kind: "image", width: 800, height: 400, version: "v2" };
  await watch.changed();
  await expect(tile.locator("img")).toHaveAttribute("src", /version=v2/);
  expect(await page.evaluate(() => {
    const tile = (window as unknown as { tile: Element }).tile;
    return tile.isConnected && tile.contains(document.activeElement);
  })).toBe(true);
});

// 回归：「已不存在」的胶囊曾用 disabled，Tab 键到不了，只用键盘时读不到原因。
test("打不开的胶囊仍能用 Tab 聚焦并显示提示条，点击无效", async ({ page, request }) => {
  await stubWorkspaceFiles(page, { files: { "gone.md": "missing" } });
  await open(page, request, deliverablesOnly([file("gone.md", `${ROOT}/gone.md`, "说明文字")]));
  const button = capsule(page, "gone.md").getByRole("button").first();
  await expect(capsule(page, "gone.md")).toContainText("已不存在");
  await expect(button).toBeDisabled();
  await button.focus();
  await expect(button).toBeFocused();
  await expect(page.locator('[data-slot="tooltip-content"]')).toContainText("gone.md");
  await button.click({ force: true });
  await expect(page.getByTestId("workspace-files-surface")).toHaveCount(0);
});

test("胶囊菜单：macOS 上有快速查看，选中即交给宿主", async ({ page, request }) => {
  await installFakeDesktopHost(page, { platform: "darwin" });
  await open(page, request, deliveredState());
  await capsule(page, "report.md").hover();
  await capsule(page, "report.md").getByRole("button", { name: "更多操作 report.md" }).click();
  await expect(page.getByRole("menuitem")).toHaveText(["快速查看", "用系统应用打开", "在文件夹中显示"]);
  await page.getByRole("menuitem", { name: "快速查看" }).click();
  await expect.poll(async () => (await fakeBrowserCalls(page)).filter((call) => call.method.startsWith("files."))).toEqual([
    { method: "files.quickLook", args: [`${ROOT}/docs/report.md`] },
  ]);
});

test("侧栏「产出」卡列出整个会话的交付物：去重、最近在前、超过五条收进显示更多，点击进文件面板", async ({ page, request }) => {
  const turn = (index: number, deliverables: TransportDeliverable[]) => {
    const part: TransportMessagePart = { ...(FILES as Extract<TransportMessagePart, { type: "tool-call" }>), toolCallId: `call-${index}`, args: { items: deliverables.map((item) => ({ target: item.key })) }, deliverables };
    return completedState(index, { id: `u-${index}`, parts: [{ type: "text", text: `第 ${index} 次`, status: "completed" }] }, [part, { type: "text", text: `交付 ${index}`, status: "completed", finalReply: true }]);
  };
  const doc = (name: string) => file(name, `${ROOT}/${name}`);
  const first = turn(1, [doc("a.md"), doc("b.md"), doc("c.md")]);
  const second = turn(2, [doc("d.md"), doc("e.md"), doc("f.md"), doc("docs/report.md"), doc("a.md")]);
  await seedState(request, SESSION_ID, { ...second, messages: [...first.messages, ...second.messages], activity: { ...second.activity!, launch: LAUNCH } });
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto(`/sessions/${SESSION_ID}`);
  const card = page.getByRole("region", { name: "产出" });
  const rows = card.locator('[data-slot="aside-deliverable"]');
  // a.md 交付了两次，只留一条、排在最近一次的位置；去重后共 7 条，先列 5 条。
  await expect(rows).toHaveText(["MDa.md", "MDreport.md", "MDf.md", "MDe.md", "MDd.md"]);
  await card.getByRole("button", { name: "显示更多（2）" }).click();
  await expect(rows).toHaveCount(7);
  await rows.filter({ hasText: "report.md" }).click();
  await expect(page.getByTestId("workspace-files-surface").getByRole("tab", { name: /report\.md/ })).toBeVisible();
});

/** 一次交付：调用带这批的说明（展示数据 `note`），回合里还有一段过程。 */
const presentCall = (id: string, deliverables: TransportDeliverable[], note: string | null): TransportMessagePart => ({
  ...(FILES as Extract<TransportMessagePart, { type: "tool-call" }>), toolCallId: id,
  args: { items: deliverables.map((item) => ({ target: item.key })), note }, display_data: { kind: "deliverables", note }, deliverables,
});
const READ: TransportMessagePart = {
  type: "tool-call", toolCallId: "call-read", toolName: "read_file", presentation: "activity", args: { path: "docs/report.md" },
  intent: "看报告", status: "completed", error: null, awaitingUser: false, display_data: null,
};
const slots = (page: Page) => page.locator('[data-role="assistant"] [data-slot="turn"] > div:not([hidden])').evaluateAll((nodes) =>
  nodes.map((node) => node.getAttribute("data-slot") ?? "").filter((slot) => ["prose-part", "deliverables-part", "process-part"].includes(slot)));

// 目的：交付卡带说明、排在最终回复之上，过程收起也在；潜在缺陷：卡片落在回复下方、随过程折叠消失，或说明没显示。
test("一次交付：说明在卡片顶部，卡片在最终回复之上，过程收起时紧贴回复", async ({ page, request }) => {
  const state = completedState(1, { id: "u-1", parts: [{ type: "text", text: "写一份报告", status: "completed" }] }, [
    READ, { type: "text", text: "先看一下现有的。", status: "completed" },
    presentCall("p1", [DELIVERABLES[0], DELIVERABLES[3]], "报告与原始数据；表格已按点赞数排序。"),
    { type: "text", text: "报告写好了。", status: "completed", finalReply: true },
  ]);
  await open(page, request, state);
  const card = page.locator('[data-slot="deliverables-card"]');
  await expect(card.locator('[data-slot="deliverables-note"]')).toHaveText("报告与原始数据；表格已按点赞数排序。");
  await expect(card).toHaveAccessibleName("报告与原始数据；表格已按点赞数排序。");
  expect(await slots(page)).toEqual(["deliverables-part", "prose-part"]);
  await page.locator('[data-slot="process-head"]').click();
  expect(await slots(page)).toEqual(["process-part", "prose-part", "deliverables-part", "prose-part"]);
});

// 目的：多次交付一次一张卡、各带各的说明；潜在缺陷：合成一张后说明对不上，重交的文件出现两次，或答案与交付写在一起时卡片夹在答案下面。
test("多次交付：每次一张卡、各带说明，重交的成果只在后一张；答案与交付同一条回复时卡片也在回复之上", async ({ page, request }) => {
  const state = completedState(1, { id: "u-1", parts: [{ type: "text", text: "改页面", status: "completed" }] }, [
    READ, presentCall("p1", [DELIVERABLES[2], DELIVERABLES[0]], "本地起好的预览站。"),
    { type: "text", text: "完整答案在这里。", status: "completed", finalReply: true },
    presentCall("p2", [DELIVERABLES[0], DELIVERABLES[1]], "改完的报告和构建产物。"),
    { type: "text", text: "想细看告诉我。", status: "completed", finalReply: true },
  ]);
  await seedState(request, SESSION_ID, state);
  await page.goto(`/sessions/${SESSION_ID}`);
  await expect(page.getByText("想细看告诉我。")).toBeVisible();
  const cards = page.locator('[data-slot="deliverables-card"]');
  await expect(cards.locator('[data-slot="deliverables-note"]')).toHaveText(["本地起好的预览站。", "改完的报告和构建产物。"]);
  await expect(cards.nth(0).locator('[data-slot="deliverable-capsule"]')).toHaveCount(1);
  await expect(cards.nth(1).locator('[data-slot="deliverable-capsule"]')).toHaveCount(2);
  expect(await slots(page)).toEqual(["deliverables-part", "deliverables-part", "prose-part", "prose-part"]);
  await page.screenshot({ path: test.info().outputPath("multiple.png"), animations: "disabled" });
});

// 目的：交付那一刻卡片就出现；潜在缺陷：运行中看不到交付物，要等回合结束才冒出来。
test("运行中交付卡即时出现在调用的位置", async ({ page, request }) => {
  const done = completedState(1, { id: "u-1", parts: [{ type: "text", text: "写一份报告", status: "completed" }] }, [
    READ, presentCall("p1", [DELIVERABLES[0]], "报告初稿。"), { type: "text", text: "正在写总结", status: "running" },
  ]);
  const [user, reply] = done.messages;
  const running = { ...done, messages: [user, { ...reply, outcome: null }], activity: { ...done.activity!, status: "running" as const, endReason: null } };
  await request.post(`${origin}/__test__/seed-state`, { data: { sessionId: SESSION_ID, hold: true, state: running } });
  await page.goto(`/sessions/${SESSION_ID}`);
  await expect(page.locator('[data-slot="deliverables-note"]')).toHaveText("报告初稿。");
  // 末尾那行是运行中的当前状态（还在想），卡片在它和正文之上、调用的位置。
  expect(await slots(page)).toEqual(["process-part", "deliverables-part", "prose-part", "process-part"]);
});
