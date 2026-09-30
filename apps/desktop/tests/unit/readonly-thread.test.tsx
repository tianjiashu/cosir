import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const captured = vi.hoisted(() => ({
  thread: null as Record<string, unknown> | null,
}));

vi.mock("@/components/assistant-ui/elements/thread.aui", () => ({
  Thread: (props: Record<string, unknown>) => {
    captured.thread = props;
    return null;
  },
}));

import { ReadonlyThread } from "@/components/assistant-ui/elements/readonly-thread.aui";

describe("ReadonlyThread", () => {
  beforeEach(() => {
    captured.thread = null;
  });

  // 目的：只读面板复用调用方 runtime 渲染 Thread，不切换只读 core。潜在缺陷：若再套一层只
  // 携带 messages 的只读 core，会遮蔽 external state，使 thread.state 恒为 null，Thread
  // 子树中读取 Transport state 的 selector 在渲染期抛错并导致整屏渲染失败。
  it("复用调用方 runtime 渲染 Thread，且不提供 composer 写入能力", () => {
    renderToStaticMarkup(<ReadonlyThread taskId={501} />);

    expect(captured.thread).toMatchObject({ readonly: true, autoFocus: false, taskId: 501 });
  });

  // 目的：只读作用域不得再引入 ReadonlyThreadProvider。潜在缺陷：其 ReadonlyThreadRuntimeCore
  // 只承载 messages、不承载 external state，会让 thread.state 恒为 null，使 Thread 子树中读
  // Transport state 的 selector 在渲染期抛错（整屏渲染失败）。
  it("不再引入会遮蔽 external state 的只读 core", () => {
    const source = readFileSync(
      new URL("../../components/assistant-ui/elements/readonly-thread.aui.tsx", import.meta.url),
      "utf8",
    );

    // 该文件只应依赖 Thread；一旦重新引入 SDK 的 runtime provider（注释中的说明性文字除外），
    // 就说明遮蔽层回来了。
    expect(source).not.toContain("@assistant-ui/react");
  });
});
