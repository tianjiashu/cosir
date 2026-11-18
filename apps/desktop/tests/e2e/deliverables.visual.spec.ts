import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import type { TransportDeliverable, TransportMessagePart, TransportState } from "../../lib/assistant/contract";
import { API_ORIGIN } from "./e2e-env";
import { installFakeDesktopHost } from "./fake-desktop-host";
import { completedState, seedState } from "./transport-fixtures";
import { stubWorkspaceFiles, type StubFile } from "./workspace-files-stubs";

/**
 * 对话里交付物胶囊的视觉基线（设计 §11.1–11.2、阶段二计划 §3.1 V1–V7），深浅两套；规则见 `playwright.visual.config.ts`。
 * 二a 只有交付时的记录，不现读文件，所以用内存测试服务注入快照即可，不需要真实后端。
 */
const SESSION_ID = 42;
const ROOT = "C:/demo";
const LAUNCH = { modelConfigId: "demo/demo-model", reasoningEffort: null };

const file = (workspacePath: string | null, absolutePath: string, size: number, caption: string | null = null): TransportDeliverable =>
  ({ type: "file", key: absolutePath, caption, workspacePath, absolutePath, kind: "text", size, width: null, height: null });
const COMMON: TransportDeliverable[] = [
  file("季度复盘报告.md", `${ROOT}/季度复盘报告.md`, 12_400, "结论在第 2 节"),
  file("data/达人投放明细_2026Q3_去重后_含退款订单_v2.csv", `${ROOT}/data/达人投放明细_2026Q3_去重后_含退款订单_v2.csv`, 325_632, "1,240 行，已去重"),
  { type: "directory", key: `${ROOT}/dist`, caption: null, workspacePath: "dist", absolutePath: `${ROOT}/dist` },
  { type: "url", key: "http://localhost:5173/", caption: "本地预览", url: "http://localhost:5173" },
];
const OUTSIDE: TransportDeliverable[] = [
  file(null, "/Users/demo/Desktop/供应商报价单.xlsx", 47_104, "工作区外"),
  file(null, "/Users/demo/Desktop/预览.html", 3_100),
];

function state(deliverables: TransportDeliverable[], withOffer = false): TransportState {
  const present: TransportMessagePart = {
    type: "tool-call", toolCallId: "call-present", toolName: "present_deliverables", presentation: "quiet",
    args: { items: deliverables.map((item) => ({ target: item.key })) }, intent: null, status: "completed", error: null, awaitingUser: false,
    deliverables,
  };
  const offer: TransportMessagePart = {
    type: "tool-call", toolCallId: "call-offer", toolName: "offer_next_step", presentation: "quiet",
    args: { ask: "要导出成 PDF 吗？", replies: ["好，导出", "先不用"] }, intent: null, status: "completed", error: null, awaitingUser: false,
    display_data: { kind: "next-step", ask: "要导出成 PDF 吗？", replies: ["好，导出", "先不用"] },
  };
  const base = completedState(1, { id: "u-1", parts: [{ type: "text", text: "整理成报告", status: "completed" }] }, [
    present, ...(withOffer ? [offer] : []), { type: "text", text: "报告整理好了，结论在第 2 节。", status: "completed", finalReply: true },
  ]);
  return { ...base, activity: { ...base.activity!, launch: LAUNCH } };
}

const FIXTURES = path.join(__dirname, "fixtures/deliverables");
/** 图片的交付记录带交付时的宽高（与现读的一致）；已不存在的图记录里也有宽高。 */
const image = (name: string): TransportDeliverable => {
  const stub = STUB_FILES[name];
  const size = typeof stub === "object" && stub.width ? { width: stub.width, height: stub.height! } : { width: 640, height: 400 };
  return { ...file(name, `${ROOT}/${name}`, 2048), kind: "image", ...size } as TransportDeliverable;
};
/** 卡片现读的文件信息：大小与交付时一致，基线不因现读改变；图片给出原图宽高。 */
const STUB_FILES: Record<string, StubFile> = {
  "季度复盘报告.md": { size: 12_400 },
  "data/达人投放明细_2026Q3_去重后_含退款订单_v2.csv": { size: 325_632 },
  "chart.png": { size: 1413, kind: "image", width: 640, height: 400 },
  "photo.png": { size: 3387, kind: "image", width: 800, height: 600 },
  "long.png": { size: 2789, kind: "image", width: 300, height: 1200 },
  "gone.png": "missing",
};
const IMAGES = Object.fromEntries(["chart.png", "photo.png", "long.png"].map((name) => [name, path.join(FIXTURES, name)]));

async function open(page: Page, theme: "light" | "dark", snapshot: TransportState, options: { host?: boolean; width?: number; aside?: boolean } = {}) {
  await page.setViewportSize({ width: options.width ?? 1280, height: 800 });
  await page.addInitScript(([preference, asideOpen]) => {
    localStorage.clear();
    localStorage.setItem("cosir:theme", JSON.stringify({ state: { preference }, version: 0 }));
    // 胶囊与方块的基线固定对话列宽：会话有交付物时侧栏「产出」卡会出现并占右列，这些用例先关掉侧栏，侧栏卡由 V13 单独录。
    if (!asideOpen) localStorage.setItem("cosir:conversation-aside", JSON.stringify({ state: { open: false, order: ["background-tasks", "deliverables"], collapsed: {} }, version: 0 }));
  }, [theme, options.aside ?? false] as const);
  if (options.host ?? true) await installFakeDesktopHost(page);
  await stubWorkspaceFiles(page, { files: STUB_FILES, images: IMAGES });
  await seedState(page.request, SESSION_ID, snapshot);
  await page.goto(`/sessions/${SESSION_ID}`);
  // 多次交付时有多张卡；单卡用例返回的就是唯一那张。
  const card = page.locator('[data-slot="deliverables-card"]').first();
  await expect(card).toBeVisible();
  // 鼠标移开，避免落在某枚胶囊上触发悬停态。
  await page.mouse.move(0, 0);
  return card;
}

/** 提示条挂在 portal 里、出现在胶囊下方，截图要把它和卡片一起框进来。 */
async function clipAround(page: Page, selector: string, pad = { top: 16, side: 24, bottom: 72 }) {
  const box = (await page.locator(selector).boundingBox())!;
  return { x: Math.max(0, box.x - pad.side), y: Math.max(0, box.y - pad.top), width: box.width + pad.side * 2, height: box.height + pad.top + pad.bottom };
}

const capsule = (page: Page, name: string) => page.locator('[data-slot="deliverable-capsule"]').filter({ hasText: name });

for (const theme of ["light", "dark"] as const) {
  test.describe(theme, () => {
    test.beforeEach(async ({ request }) => {
      await request.post(`${API_ORIGIN}/__test__/reset`);
      await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: SESSION_ID, title: "交付物" } });
    });

    test("V1 常见组合：长名中间截断、目录、本机网址", async ({ page }) => {
      const card = await open(page, theme, state(COMMON));
      await expect(card).toHaveScreenshot(`common-${theme}.png`);
    });

    test("V2/V3 悬停与键盘焦点：次按钮叠在大小一格、宽度不变、提示条两行", async ({ page }) => {
      const card = await open(page, theme, state(COMMON));
      const target = capsule(page, "_v2.csv");
      const width = (await target.boundingBox())!.width;
      await target.getByRole("button").first().hover();
      await expect(page.locator('[data-slot="tooltip-content"]')).toContainText("1,240 行");
      expect((await target.boundingBox())!.width).toBe(width);
      await expect(page).toHaveScreenshot(`hover-${theme}.png`, { clip: await clipAround(page, '[data-slot="deliverables-card"]') });

      await page.mouse.move(0, 0);
      // 次按钮平时不可见、不能直接聚焦；从前一枚的主按钮按 Tab 经过它（焦点在胶囊内它才出现），顺带验证键盘路径。
      const previous = capsule(page, "季度复盘报告");
      await previous.getByRole("button").first().focus();
      await page.keyboard.press("Tab");
      await expect(previous.getByRole("button", { name: /更多操作/ })).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(target.getByRole("button").first()).toBeFocused();
      await expect(card).toHaveScreenshot(`focus-main-${theme}.png`);
      await page.keyboard.press("Tab");
      await expect(target.getByRole("button", { name: /更多操作/ })).toBeFocused();
      await expect(card).toHaveScreenshot(`focus-reveal-${theme}.png`);
    });

    test("V12 胶囊的出口菜单（macOS）", async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 800 });
      await installFakeDesktopHost(page, { platform: "darwin" });
      await open(page, theme, state(COMMON), { host: false });
      const target = capsule(page, "季度复盘报告");
      await target.hover();
      await target.getByRole("button", { name: /更多操作/ }).click();
      await expect(page.getByRole("menuitem")).toHaveCount(3);
      await expect(page).toHaveScreenshot(`exit-menu-${theme}.png`, { clip: await clipAround(page, '[data-slot="deliverables-card"]') });
    });

    test("V4 工作区外的条目：主操作是在文件夹中显示，也有出口菜单；大小是交付时的值", async ({ page }) => {
      const card = await open(page, theme, state(OUTSIDE));
      await capsule(page, "供应商报价单").hover();
      await expect(capsule(page, "供应商报价单").getByRole("button")).toHaveCount(2);
      await page.mouse.move(0, 0);
      await expect(card).toHaveScreenshot(`outside-${theme}.png`);
    });

    test("V5 回合顺序：交付物 → 最终回复 → 下一步建议", async ({ page }) => {
      await open(page, theme, state(COMMON.slice(0, 2), true));
      const reply = page.locator('[data-role="assistant"]').last();
      await expect(reply.locator('[data-slot="next-step-card"]')).toBeVisible();
      await expect(reply).toHaveScreenshot(`turn-order-${theme}.png`);
    });

    test("V14 交付说明与多次交付：每次一张卡、说明在卡片顶部，叠在最终回复之上", async ({ page }) => {
      const noted = (id: string, deliverables: TransportDeliverable[], note: string): TransportMessagePart => ({
        type: "tool-call", toolCallId: id, toolName: "present_deliverables", presentation: "quiet",
        args: { items: deliverables.map((item) => ({ target: item.key })), note }, intent: null, status: "completed", error: null, awaitingUser: false,
        display_data: { kind: "deliverables", note }, deliverables,
      });
      const base = completedState(1, { id: "u-1", parts: [{ type: "text", text: "整理成报告", status: "completed" }] }, [
        noted("p1", [COMMON[3]], "本地起好的预览站，改完的三个页面都能点进去看。"),
        noted("p2", [COMMON[0], COMMON[1]], "季度复盘报告和它用到的投放明细；明细已按达人去重，退款订单单列在最后一个工作表。"),
        { type: "text", text: "报告整理好了，结论在第 2 节。", status: "completed", finalReply: true },
      ]);
      await open(page, theme, { ...base, activity: { ...base.activity!, launch: LAUNCH } });
      const reply = page.locator('[data-role="assistant"]').last();
      await expect(reply.locator('[data-slot="deliverables-note"]')).toHaveCount(2);
      await expect(reply).toHaveScreenshot(`notes-${theme}.png`);
    });

    test("V6 无宿主的开发模式：没有次按钮", async ({ page }) => {
      const card = await open(page, theme, state([...COMMON, ...OUTSIDE]), { host: false });
      await capsule(page, "季度复盘报告").hover();
      await expect(page.locator('[data-slot="deliverable-capsule"] button[aria-label^="更多操作"]')).toHaveCount(0);
      await expect(card).toHaveScreenshot(`no-host-${theme}.png`);
    });

    test("V8/V9 图片方块：缩略图、长图裁切保留顶部、已不存在的图保持方块；胶囊另起一行", async ({ page }) => {
      const card = await open(page, theme, state([image("chart.png"), image("photo.png"), image("long.png"), image("gone.png"), COMMON[0]]));
      const tiles = card.locator('[data-slot="image-tile"] img');
      await expect(tiles).toHaveCount(3);
      for (const index of [0, 1, 2]) await expect.poll(() => tiles.nth(index).evaluate((element: HTMLImageElement) => element.complete && element.naturalWidth > 0)).toBe(true);
      await expect(card).toHaveScreenshot(`tiles-${theme}.png`);
    });

    test("V11 单张大图卡：横图按比例装进 360×240；竖长图装进去比方块还窄，退回方块；与胶囊同组", async ({ page }) => {
      for (const [name, label, selector] of [["chart.png", "wide", '[data-slot="deliverable-image-card"] img'], ["long.png", "tall", '[data-slot="image-tile"] img']] as const) {
        const card = await open(page, theme, state([image(name), COMMON[0]]));
        const picture = card.locator(selector);
        await expect.poll(() => picture.evaluate((element: HTMLImageElement) => element.complete && element.naturalWidth > 0)).toBe(true);
        await expect(card).toHaveScreenshot(`single-${label}-${theme}.png`);
      }
    });

    test("V13 侧栏「产出」卡：角标加名称、中间截断、显示更多", async ({ page }) => {
      const many = [...COMMON, ...OUTSIDE, file("notes/2026Q3_投放复盘_最终版_含附录与数据口径说明.md", `${ROOT}/notes/2026Q3_投放复盘_最终版_含附录与数据口径说明.md`, 900)];
      await open(page, theme, state(many), { width: 1600, aside: true });
      const card = page.getByRole("region", { name: "产出" });
      await expect(card.locator('[data-slot="aside-deliverable"]')).toHaveCount(5);
      await page.mouse.move(0, 0);
      await expect(card).toHaveScreenshot(`aside-${theme}.png`);
    });

    test("V7 窄列宽换行", async ({ page }) => {
      const card = await open(page, theme, state([...COMMON, ...OUTSIDE]), { width: 640 });
      await expect(card).toHaveScreenshot(`narrow-${theme}.png`);
    });
  });
}
