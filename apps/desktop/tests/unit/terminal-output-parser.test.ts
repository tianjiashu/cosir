import { Terminal } from "@xterm/xterm";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TerminalOutputParser } from "@/components/conversation/tools/terminal-output/output-parser";
import type { OutputTail } from "@/components/conversation/tools/terminal-output/text";

const lineText = (tail: OutputTail) => tail.lines.map((runs) => runs.map((run) => run.text).join(""));

function parserWithTails() {
  const tails: OutputTail[] = [];
  const waiters: Array<() => void> = [];
  const parser = new TerminalOutputParser((tail) => {
    tails.push(tail);
    waiters.splice(0).forEach((resolve) => resolve());
  }, (error) => { throw error; });
  const nextTail = () => new Promise<void>((resolve) => waiters.push(resolve));
  return { parser, tails, nextTail };
}

describe("TerminalOutputParser", () => {
  afterEach(() => vi.restoreAllMocks());

  it("累计输出是前缀时只写新增的后缀，不重写已解析的部分", async () => {
    const write = vi.spyOn(Terminal.prototype, "write");
    const { parser, tails, nextTail } = parserWithTails();
    let ready = nextTail();
    parser.update("one\ntwo\n");
    await ready;
    ready = nextTail();
    parser.update("one\ntwo\nthree\n");
    await ready;

    expect(write.mock.calls.map(([data]) => data)).toEqual(["one\ntwo\n", "three\n"]);
    expect(lineText(tails.at(-1)!)).toEqual(["one", "two", "three"]);
    expect(parser.text()).toBe("one\ntwo\nthree");
    parser.dispose();
  });

  it("开头被裁掉（不再是前缀）时重置后整段重写", async () => {
    const reset = vi.spyOn(Terminal.prototype, "reset");
    const { parser, tails, nextTail } = parserWithTails();
    let ready = nextTail();
    parser.update("old-1\nold-2\n");
    await ready;
    ready = nextTail();
    parser.update("old-2\nnew\n");
    await ready;

    expect(reset).toHaveBeenCalledTimes(1);
    expect(lineText(tails.at(-1)!)).toEqual(["old-2", "new"]);
    parser.dispose();
  });

  it("解析期间到达的多次更新合并：只按最新的累计输出写入", async () => {
    const write = vi.spyOn(Terminal.prototype, "write");
    const { parser, tails, nextTail } = parserWithTails();
    let ready = nextTail();
    parser.update("a\n");
    parser.update("a\nb\n");
    parser.update("a\nb\nc\n");
    await ready;
    if (lineText(tails.at(-1)!).length < 3) {
      ready = nextTail();
      await ready;
    }

    expect(write.mock.calls.length).toBeLessThanOrEqual(2);
    expect(lineText(tails.at(-1)!)).toEqual(["a", "b", "c"]);
    parser.dispose();
  });
});
