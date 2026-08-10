// @vitest-environment happy-dom
/**
 * 缺陷验证 #7：ThinkingBlock 「mounted」日志 effect 依赖 content，流式期日志洪峰。
 *
 * 背景：ThinkingBlock 约 68-76 行的日志 effect 语义是「挂载时记录一次」
 * （日志名 thinking_block_mounted），但依赖数组为 [content, isEmpty, streaming]。
 * 流式期间 content 每个 token 都变化，effect 每次重跑 → logInfo 每次调用，
 * 经 logger → Tauri IPC 落盘，形成日志洪峰（N 个 token = N 次 IPC）。
 * 正确行为：真 mounted 语义，同一组件实例只记录一次。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

vi.mock("@/lib/logger", () => ({
  logInfo: vi.fn(),
  logWarn: vi.fn(),
  logError: vi.fn(),
  logDebug: vi.fn(),
}));

import { logInfo } from "@/lib/logger";
import { ThinkingBlock } from "@/components/chat/ThinkingBlock";

describe("ThinkingBlock 挂载日志频率", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // 测试目的：流式 content 多次变化时，名为 mounted 的日志只应记录一次。
  // 可能发现的缺陷：日志 effect 依赖 content，每个流式分片都触发 logInfo → IPC 洪峰。
  it("流式 content 连续追加时，thinking_block_mounted 日志只应调用 1 次", () => {
    const { rerender } = render(<ThinkingBlock content="思考片段一" />);
    rerender(<ThinkingBlock content="思考片段一二" />);
    rerender(<ThinkingBlock content="思考片段一二三" />);
    rerender(<ThinkingBlock content="思考片段一二三四" />);

    // 正确行为：mounted 语义 = 每实例 1 次。
    expect(vi.mocked(logInfo)).toHaveBeenCalledTimes(1);
  });

  // 测试目的：正向对照——content 不变的重渲染不应重复记录（effect 依赖未变）。
  // 可能发现的缺陷：无（此用例应 PASS，证明 mock 与渲染链路正确）。
  it("对照：content 不变的重渲染不重复记录日志", () => {
    const { rerender } = render(<ThinkingBlock content="固定内容" />);
    rerender(<ThinkingBlock content="固定内容" />);
    expect(vi.mocked(logInfo)).toHaveBeenCalledTimes(1);
  });
});
