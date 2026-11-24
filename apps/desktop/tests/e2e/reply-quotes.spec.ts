import { expect, test, type Page } from "@playwright/test";

import { API_ORIGIN } from "./e2e-env";
import { completedState, seedState } from "./transport-fixtures";
import { dragSelect, dragSelectElement } from "./text-selection-helpers";

const REPLY = [
  "## 方案",
  "",
  "第一段说明文字，包含**加粗**内容。",
  "",
  "1. 第一项",
  "2. 第二项",
  "3. 第三项",
  "",
  "| 操作 | 行为 |",
  "|---|---|",
  "| 回车 | 插入 |",
  "| Esc | 关闭 |",
  "| 点外面 | 不关 |",
  "",
  "```ts",
  "const a = 1;",
  "const b = 2;",
  "```",
].join("\n");

test.beforeEach(async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  await request.post(`${API_ORIGIN}/__test__/seed-session`, { data: { sessionId: 100, title: "摘录" } });
  await seedState(request, 100, completedState(70, {
    id: "history-user",
    parts: [{ type: "text", text: "给个方案", status: "completed" }],
  }, [{ type: "text", text: REPLY, status: "completed" }]));
  await page.goto("/sessions/100");
  await expect(page.getByLabel("消息输入", { exact: true })).toBeEditable();
  await expect(page.locator('[data-role="assistant"] table')).toBeVisible();
});

/** 在回复里用真实鼠标从 `from` 的开头拖到 `to` 的结尾。 */
async function select(page: Page, from: string, to: string) {
  await dragSelect(page, page.locator('[data-role="assistant"]'), from, to);
}

/** 摘录进输入框：点「摘录」、写批注、回车；评论框关掉、输入框上方多一条摘录。 */
async function quote(page: Page, note: string) {
  const cards = page.locator('[data-slot="composer-quotes"] [data-quote-bar]');
  const before = await cards.count();
  await page.getByRole("button", { name: "摘录", exact: true }).click();
  const box = page.getByLabel("写批注");
  await box.fill(note);
  await box.press("Enter");
  await expect(box).toHaveCount(0);
  await expect(cards).toHaveCount(before + 1);
}

type SentQuote = { text: string; note?: string };
const ENTITIES: [string, string][] = [["&#126;", "~"], ["&#96;", "`"], ["&#9;", "\t"], ["&#13;", "\r"], ["&#10;", "\n"], ["&quot;", '"'], ["&gt;", ">"], ["&lt;", "<"], ["&amp;", "&"]];
const unescape = (value: string) => ENTITIES.reduce((text, [entity, raw]) => text.replaceAll(entity, raw), value);

/** 点发送，从请求正文里读出摘录胶囊与其余正文（摘录的事实就是正文里的胶囊）。 */
async function send(page: Page): Promise<{ quotes: SentQuote[]; rest: string }> {
  const request = page.waitForRequest((req) => req.url().endsWith("/sessions/100/messages") && req.method() === "POST");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  const text = ((await request).postDataJSON() as { message: { parts: { text: string }[] } }).message.parts[0]!.text;
  const quotes = [...text.matchAll(/<quote ([^>]*?) \/>/g)].map((match) =>
    Object.fromEntries([...match[1]!.matchAll(/([a-z]+)="([^"]*)"/g)].map(([, key, value]) => [key, unescape(value!)])) as SentQuote);
  return { quotes, rest: text.replace(/<quote [^>]*? \/>\n?/g, "") };
}

test("选区跨段落与列表：没选全的段落取选中文字，列表取选中的项并保留编号；批注与原文同在一个胶囊", async ({ page }) => {
  await select(page, "说明文字", "第二项");
  await quote(page, "这两点展开说说");
  await expect(page.locator("[data-quote-bar]")).toContainText("带批注");
  expect((await send(page)).quotes).toEqual([{ text: "说明文字，包含加粗内容。\n\n1. 第一项\n2. 第二项", note: "这两点展开说说" }]);
});

test("整块选中取 Markdown 源文；表格只选两行时带上表头重组；多条摘录按添加顺序", async ({ page }) => {
  await select(page, "方案", "内容。");
  await quote(page, "");
  await select(page, "回车", "关闭");
  await quote(page, "");
  const { quotes } = await send(page);
  expect(quotes.map((item) => item.text)).toEqual([
    "## 方案\n\n第一段说明文字，包含**加粗**内容。",
    "| 操作 | 行为 |\n|---|---|\n| 回车 | 插入 |\n| Esc | 关闭 |",
  ]);
  expect(quotes.every((item) => item.note === undefined)).toBe(true);
});

test("代码只选一行：保留围栏与语言", async ({ page }) => {
  // 代码高亮完成后一行会拆成多个词元，按行元素拖选。高亮是异步的，词元出现后代码块还可能再重绘一次：
  // 等这一行的元素连续 300ms 不被替换再拖，否则选区落到重绘前的结构上（曾选进上一行，或元素已脱离文档）。
  const line = page.locator('[data-role="assistant"] [data-streamdown="code-block"] pre > code > span').nth(1);
  await expect(line.locator("span[style]").first()).toBeVisible();
  await expect.poll(() => line.evaluate((element) => new Promise((settle) => setTimeout(() => settle(element.isConnected), 300)))).toBe(true);
  await dragSelectElement(page, line);
  await quote(page, "");
  expect((await send(page)).quotes[0]?.text).toBe("```ts\nconst b = 2;\n```");
});

// 目的：浮条与批注卡不盖住正往下读的内容；潜在缺陷：浮层出在选区下方挡住后文，或只能靠回车提交。
test("浮条在选区第一行上方，批注卡原位展开；发送按钮与回车一样能添加", async ({ page }) => {
  await select(page, "第一项", "第三项");
  const firstLine = await page.locator('[data-role="assistant"] li').first().boundingBox();
  const bar = page.locator('[data-slot="selection-toolbar"]');
  // 弹层从下方滑入（data-side=top 时起点下移 8px）：等入场动画结束再量位置。
  const settled = () => bar.evaluate((element) => Promise.all(element.getAnimations().map((animation) => animation.finished)));
  await expect(bar).toContainText(/⌘L|Ctrl\+L/);
  await settled();
  expect((await bar.boundingBox())!.y + (await bar.boundingBox())!.height).toBeLessThanOrEqual(firstLine!.y);
  await page.screenshot({ path: test.info().outputPath("selection-toolbar.png") });

  await page.getByRole("button", { name: "摘录", exact: true }).click();
  await expect(page.getByLabel("写批注")).toBeVisible();
  await settled();
  const card = await bar.boundingBox();
  expect(card!.y + card!.height).toBeLessThanOrEqual(firstLine!.y);
  await page.getByLabel("写批注").fill("按钮提交");
  await page.screenshot({ path: test.info().outputPath("selection-comment-card.png") });
  await page.getByRole("button", { name: "添加到输入框" }).click();
  await expect(page.getByLabel("写批注")).toHaveCount(0);
  expect((await send(page)).quotes).toEqual([{ text: "1. 第一项\n2. 第二项\n3. 第三项", note: "按钮提交" }]);
});

test("Esc 放弃：不加摘录", async ({ page }) => {
  await select(page, "第一项", "第一项");
  await page.getByRole("button", { name: "摘录", exact: true }).click();
  await page.getByLabel("写批注").fill("不要了");
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("写批注")).toHaveCount(0);
  await expect(page.locator("[data-quote-bar]")).toHaveCount(0);
});

test("点摘录条在对话框里改批注、✕ 删除，都改正文；与正文一起发出", async ({ page }) => {
  await select(page, "第一项", "第一项");
  await quote(page, "旧批注");
  await select(page, "第三项", "第三项");
  await quote(page, "删掉我");
  const cards = page.locator('[data-slot="composer-quotes"] [data-quote-bar]');
  await cards.nth(0).getByRole("button", { name: /^查看摘录/ }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("第一项");
  await dialog.getByLabel("摘录批注").fill("新批注");
  await dialog.getByLabel("摘录批注").press("Enter");
  await expect(dialog).toHaveCount(0);
  await cards.nth(1).getByRole("button", { name: /^移除摘录/ }).click();
  await expect(cards).toHaveCount(1);
  await page.getByLabel("消息输入", { exact: true }).click();
  await page.keyboard.type("整体再看看");
  const sent = await send(page);
  expect(sent.quotes).toEqual([{ text: "1. 第一项", note: "新批注" }]);
  expect(sent.rest).toBe("整体再看看");
});

test("发出后气泡里是引文（原文 + 批注）；撤回后摘录与批注原样回到输入框", async ({ page }) => {
  await select(page, "回车", "关闭");
  await quote(page, "改成列表");
  await send(page);

  const bubble = page.locator('[data-role="user"]').last();
  const block = bubble.locator("[data-quote-block]");
  await expect(block).toContainText("回车");
  await expect(block).toContainText("改成列表");

  await expect(page.locator('[data-role="assistant"]').last()).toContainText("streaming response");
  await bubble.hover();
  await bubble.getByRole("button", { name: "撤回" }).click();
  const bars = page.locator('[data-slot="composer-quotes"] [data-quote-bar]');
  await expect(bars).toHaveCount(1);
  await bars.getByRole("button", { name: /^查看摘录/ }).click();
  await expect(page.getByRole("dialog").getByLabel("摘录批注")).toHaveValue("改成列表");
});

test("Cmd/Ctrl+L 打开评论框；写了字时点到别处不关，空着点到别处就关", async ({ page }) => {
  const mod = process.platform === "darwin" ? "Meta" : "Control";
  await select(page, "第一项", "第一项");
  await expect(page.getByRole("button", { name: "摘录", exact: true })).toBeVisible();
  await page.keyboard.press(`${mod}+l`);
  const box = page.getByLabel("写批注");
  await box.fill("写了一半");
  await page.locator('[data-role="user"]').first().click();
  await expect(box).toHaveValue("写了一半");

  await box.fill("");
  await page.locator('[data-role="user"]').first().click();
  await expect(box).toHaveCount(0);
});

/**
 * 正文以代码结尾时加摘录：摘录照样成为卡片、发出后是胶囊。摘录只经卡片区合进正文（排在开头），不拼在正文末尾——
 * 拼在末尾会落进结尾的代码块（闭合围栏后面紧跟标签不再是闭合行），卡片消失、标签按字面量发给模型。
 */
const CODE_ENDINGS: Array<[string, string[]]> = [
  ["闭合的反引号围栏", ["请检查这段代码：", "```ts", "const a = 1;", "```"]],
  ["闭合的波浪线围栏", ["~~~", "x", "~~~"]],
  ["没闭合的围栏", ["```ts", "const a = 1;"]],
  ["缩进代码", ["看这段：", "", "    const a = 1;"]],
];
for (const [name, lines] of CODE_ENDINGS) {
  test(`正文以${name}结尾时加摘录：摘录成为卡片，代码原样`, async ({ page }) => {
    const input = page.getByLabel("消息输入", { exact: true });
    await input.click();
    for (const [index, line] of lines.entries()) {
      if (index) await page.keyboard.press("Shift+Enter");
      if (line) await page.keyboard.insertText(line);
    }
    await select(page, "第一段说明文字", "第一段说明文字");
    await quote(page, "解释一下");
    const sent = await send(page);
    expect(sent.quotes).toEqual([{ text: "第一段说明文字", note: "解释一下" }]);
    expect(sent.rest).toBe(lines.join("\n"));
  });
}

test("粘贴带摘录的文字进以代码结尾的正文：摘录进卡片区", async ({ page }) => {
  const input = page.getByLabel("消息输入", { exact: true });
  await input.click();
  for (const [index, line] of ["```", "x", "```"].entries()) {
    if (index) await page.keyboard.press("Shift+Enter");
    await page.keyboard.insertText(line);
  }
  await input.evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.setData("text/plain", '<quote text="粘贴的摘录" note="看看" />');
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true }));
  });
  await expect(page.locator('[data-slot="composer-quotes"] [data-quote-bar]')).toHaveCount(1);
  const sent = await send(page);
  expect(sent.quotes).toEqual([{ text: "粘贴的摘录", note: "看看" }]);
  expect(sent.rest).toBe("```\nx\n```");
});
