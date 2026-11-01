import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

// 服务端渲染读 zustand 初始状态：开合覆盖换成可控的表（store 本身的规则见 disclosure-store 单测）。
const overrides = vi.hoisted(() => new Map<string, boolean>());
vi.mock("@/lib/assistant/disclosure-store", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/assistant/disclosure-store")>(),
  useDisclosure: (_taskId: number, key: string, defaultOpen: boolean) => [overrides.get(key) ?? defaultOpen, () => undefined],
}));

import { ProcessBlock, processHeading } from "@/components/assistant-ui/turn/process-block";
import type { TransportMessagePart, TransportToolCallPart } from "@/lib/assistant/contract";
import { projectTurn, type TurnPhase } from "@/lib/assistant/turn-layout";

function call(id: string, verb: string, status: TransportToolCallPart["status"] = "completed", title?: string): TransportToolCallPart {
  return {
    type: "tool-call", toolCallId: id, toolName: verb, args: {}, status, awaitingUser: false,
    presentation: { verb, icon: "eye" },
    ...(title ? { heading: { title, target: null } } : {}),
  };
}

function render(parts: TransportMessagePart[], phase: TurnPhase, durationMs: number | null = null): string {
  const layout = projectTurn(parts, phase);
  return renderToStaticMarkup(
    <ProcessBlock
      taskId={1}
      messageId="m1"
      phase={phase}
      process={layout.process}
      durationMs={durationMs}
      renderCall={(item) => <span data-call={item.part.toolCallId}>{item.part.toolCallId}</span>}
    />,
  );
}

beforeEach(() => overrides.clear());

describe("processHeading", () => {
  it("结束后：已处理 + 耗时 + 按动词计数 + 失败 / 取消计数", () => {
    const layout = projectTurn([
      call("a", "读取文件"), call("b", "读取文件"), call("c", "执行命令", "failed", "运行测试"), call("d", "执行命令", "cancelled"),
    ], "settled");
    expect(processHeading("settled", layout.process, 192_000)).toEqual({
      lead: "已处理 3 分 12 秒",
      counts: "读取文件 2 · 执行命令 2",
      problems: "1 个失败 · 1 个取消",
    });
    expect(processHeading("settled", layout.process, null).lead).toBe("已处理");
  });

  it("运行中：当前动作是最新未结束调用的标题，没有则「思考中」；挂起写明等你操作", () => {
    const running = projectTurn([call("a", "读取文件"), call("b", "执行命令", "running", "运行测试")], "running");
    expect(processHeading("running", running.process, 5_000).lead).toBe("运行测试");
    const thinking = projectTurn([call("a", "读取文件"), { type: "reasoning", text: "想", status: "running" }], "running");
    expect(processHeading("running", thinking.process, null).lead).toBe("思考中");
    expect(processHeading("suspended", running.process, null).lead).toBe("已暂停 · 等你操作");
  });
});

describe("ProcessBlock", () => {
  it("只有一次调用时平铺，不加区头", () => {
    const html = render([{ type: "reasoning", text: "先看看", status: "completed" }, call("a", "读取文件")], "settled");
    expect(html).toContain('data-slot="process-flat"');
    expect(html).not.toContain('data-slot="process-head"');
    expect(html).toContain('data-call="a"');
  });

  it("结束后有区头且默认收起，区头文案含计数", () => {
    const html = render([call("a", "读取文件"), call("b", "搜索内容"), call("c", "读取文件")], "settled", 48_000);
    expect(html).toContain("已处理 48 秒");
    expect(html).toContain("读取文件 2 · 搜索内容 1");
    expect(html).toMatch(/aria-expanded="false"[^>]*data-slot="process-head"/);
  });

  it("运行中默认展开：当前动作闪烁，只露活跃尾部并给出「前面还有 N 步」", () => {
    const parts: TransportMessagePart[] = [
      call("a", "读取文件"), call("b", "读取文件"), call("c", "读取文件"), call("d", "搜索内容"), call("e", "读取文件"),
      call("f", "执行命令", "running", "运行单元测试"),
    ];
    const html = render(parts, "running", 3_000);
    expect(html).toMatch(/aria-expanded="true"[^>]*data-slot="process-head"/);
    expect(html).toMatch(/data-slot="process-lead"[^>]*shimmer[^>]*>运行单元测试/);
    expect(html).toContain("前面还有 2 步");
    expect(html).not.toContain('data-call="a"');
    expect(html).toContain('data-call="f"');
  });

  it("用户覆盖优先于阶段默认值", () => {
    overrides.set("process:m1", true);
    const html = render([call("a", "读取文件"), call("b", "搜索内容")], "settled");
    expect(html).toMatch(/aria-expanded="true"[^>]*data-slot="process-head"/);
    expect(html).toContain('data-call="a"');
    expect(html).toContain('data-call="b"');
  });

  it("失败计数用 destructive 色", () => {
    const html = render([call("a", "读取文件"), call("b", "执行命令", "failed")], "settled");
    expect(html).toMatch(/data-slot="process-problems"[^>]*text-destructive[^>]*>· 1 个失败/);
  });
});
