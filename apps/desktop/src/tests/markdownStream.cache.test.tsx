// @vitest-environment happy-dom
/**
 * MarkdownStream 定稿态解析缓存性能回归测试。
 *
 * 验证性能契约：定稿态（`streaming=false`）下，父组件因事件引用变化而多次重渲染时，
 * 只要 `renderedContent` 与 `components` 引用不变，`ReactMarkdown` 不应被重复调用——
 * 即 react-markdown 全量解析只发生一次，避免冷启动全量重投影时几十次同步解析阻塞主线程。
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { act } from "react";
import type { Components } from "react-markdown";

// 用计数器 spy 替换 react-markdown 的默认导出 ReactMarkdown，断言解析调用次数。
const renderCount = { ReactMarkdown: 0 };
vi.mock("react-markdown", () => ({
  default: (props: { children: string }) => {
    renderCount.ReactMarkdown += 1;
    return (
      <div data-testid="markdown">
        {typeof props.children === "string" ? props.children : ""}
      </div>
    );
  },
}));

import { MarkdownStream } from "@/components/chat/MarkdownStream";

// 定稿态 components 引用需稳定，模拟 AgentMessage 中 useMemo(() => buildMarkdownComponents(streaming), [streaming])。
const stableComponents = {} as Components;

describe("MarkdownStream 定稿态解析缓存契约", () => {
  it("同一 content 在父组件多次重渲染时只解析一次（useMemo 命中）", () => {
    renderCount.ReactMarkdown = 0;
    const content = "# 历史消息\n\n这是一段很长的定稿 Markdown 内容，不应被重复解析。";

    const { rerender } = render(
      <MarkdownStream content={content} streaming={false} components={stableComponents} />,
    );

    // 模拟冷启动从零全量投影：TurnTimeline 因 events 引用变化触发多次重渲染，
    // 但每个 assistant 块的 content 未变。
    const repaints = 5;
    for (let i = 0; i < repaints; i += 1) {
      act(() => {
        rerender(<MarkdownStream content={content} streaming={false} components={stableComponents} />);
      });
    }

    // 关键不变量：无论父层重渲染多少次，定稿态同一 content 的 ReactMarkdown 只调用一次。
    expect(renderCount.ReactMarkdown).toBe(1);
  });

  it("content 变化时必须重新解析（缓存正确失效）", () => {
    renderCount.ReactMarkdown = 0;
    const first = "第一段定稿内容";
    const second = "第二段不同的定稿内容";

    const { rerender } = render(
      <MarkdownStream content={first} streaming={false} components={stableComponents} />,
    );
    expect(renderCount.ReactMarkdown).toBe(1);

    act(() => {
      rerender(<MarkdownStream content={second} streaming={false} components={stableComponents} />);
    });

    // content 真变化 → 缓存失效 → 重新解析一次（累计 2 次）。
    expect(renderCount.ReactMarkdown).toBe(2);
  });

  it("流式态受 REPARSE_INTERVAL_MS 节流，节流窗口内不重复解析", () => {
    renderCount.ReactMarkdown = 0;
    vi.useFakeTimers();
    try {
      const { rerender, container } = render(
        <MarkdownStream content="t0" streaming={true} components={stableComponents} />,
      );
      // 首帧立即解析一次（elapsed 累计达节流点）。
      expect(renderCount.ReactMarkdown).toBe(1);

      // 节流窗口内连续高频更新 content：不应触发新的解析。
      act(() => {
        rerender(<MarkdownStream content="t0-1" streaming={true} components={stableComponents} />);
        rerender(<MarkdownStream content="t0-2" streaming={true} components={stableComponents} />);
      });
      expect(renderCount.ReactMarkdown).toBe(1);

      // 推进到节流点后，最新 content 应被解析一次（且只取最后一次）。
      act(() => {
        vi.advanceTimersByTime(150);
      });
      expect(renderCount.ReactMarkdown).toBe(2);

      // 验证流式节流不丢 token：推进后继续更新 content 并再次推进节流点，
      // 渲染输出必须反映最新（最后一次）content，而非被某次中间快照覆盖。
      act(() => {
        rerender(<MarkdownStream content="final" streaming={true} components={stableComponents} />);
        vi.advanceTimersByTime(150);
      });
      expect(renderCount.ReactMarkdown).toBe(3);
      expect(container.querySelector('[data-testid="markdown"]')?.textContent).toBe("final");
    } finally {
      vi.useRealTimers();
    }
  });
});
