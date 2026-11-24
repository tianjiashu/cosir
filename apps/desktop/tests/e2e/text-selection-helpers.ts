import type { Locator, Page } from "@playwright/test";

/**
 * 用真实鼠标从 `from` 的第一个字拖到 `to` 的最后一个字（两段文字都在 `scope` 里，按文本节点查找）。
 *
 * 选区交互必须用真实输入测：拖选结束时浏览器还会补一个 click，脚本直接 addRange 不会有它——浮条曾因此一出现
 * 就被当成「点到外面」关掉，而用合成事件的测试照样通过。
 */
export async function dragSelect(page: Page, scope: Locator, from: string, to: string) {
  const points = await scope.evaluate((root, [from, to]) => {
    const nodes: Text[] = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node as Text);
    const locate = (text: string, edge: "start" | "end") => {
      const node = nodes.find((candidate) => candidate.data.includes(text));
      if (!node) throw new Error(`找不到文字：${text}`);
      const index = node.data.indexOf(text) + (edge === "start" ? 0 : text.length - 1);
      const range = document.createRange();
      range.setStart(node, index);
      range.setEnd(node, index + 1);
      return range;
    };
    const first = locate(from, "start");
    const last = locate(to, "end");
    // 只滚一次（滚到起点），再一起量两端：量完一端再滚会让另一端的坐标作废。
    first.startContainer.parentElement!.scrollIntoView({ block: "center" });
    const point = (range: Range, edge: "start" | "end") => {
      const box = range.getBoundingClientRect();
      return { x: edge === "start" ? box.left + 1 : box.right - 1, y: box.top + box.height / 2 };
    };
    return { start: point(first, "start"), end: point(last, "end") };
  }, [from, to] as const);
  await page.mouse.move(points.start.x, points.start.y);
  await page.mouse.down();
  await page.mouse.move(points.end.x, points.end.y, { steps: 8 });
  await page.mouse.up();
}

/** 用真实鼠标从 `element` 的第一个字拖到最后一个字（如代码块的一行：高亮后一行拆成多个词元节点）。 */
export async function dragSelectElement(page: Page, element: Locator) {
  await element.scrollIntoViewIfNeeded();
  const points = await element.evaluate((root) => {
    const nodes: Text[] = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if ((node as Text).data.trim()) nodes.push(node as Text);
    const box = (node: Text, index: number) => {
      const range = document.createRange();
      range.setStart(node, index);
      range.setEnd(node, index + 1);
      return range.getBoundingClientRect();
    };
    const first = nodes[0]!;
    const last = nodes.at(-1)!;
    const start = box(first, first.data.search(/\S/));
    const end = box(last, last.data.trimEnd().length - 1);
    return { start: { x: start.left + 1, y: start.top + start.height / 2 }, end: { x: end.right - 1, y: end.top + end.height / 2 } };
  });
  await page.mouse.move(points.start.x, points.start.y);
  await page.mouse.down();
  await page.mouse.move(points.end.x, points.end.y, { steps: 8 });
  await page.mouse.up();
}
