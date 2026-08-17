// @vitest-environment happy-dom
/**
 * M8 回归：backendStore.transportError 已维护但原无组件渲染。
 *
 * 修复后 BackendErrorBanner 在：
 *  - status 为 "running" 或 "ready" 且 transportError 非空：渲染非阻塞告警条
 *    （文案含「后端连接中断」与「正在尝试恢复」）；
 *  - status 为 "failed" 且 snapshot.lastError 存在：渲染结构化错误卡，
 *    且不与 transport 告警叠加；
 *  - status 为 "ready" 且 transportError 为 null：不渲染任何告警条（返回 null）。
 *
 * 通过 useBackendStore.setState 直接驱动 store 状态，断言横幅渲染分支正确。
 *
 * @module tests/backendErrorBanner.m8
 */

import { describe, expect, it, beforeEach, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { BackendErrorBanner } from "@/components/backend/BackendErrorBanner";
import { useBackendStore } from "@/stores/backendStore";
import type { BackendStatusResponse } from "@shared/backend";

/** 构造一个结构化失败快照。 */
function makeSnapshot(lastError: { message: string; detail?: string; stack?: string; stage?: string; occurredAt?: string }): BackendStatusResponse {
  return {
    status: "failed",
    managed: false,
    pid: null,
    url: null,
    started_at: null,
    lastError: {
      type: "BootError",
      message: lastError.message,
      detail: lastError.detail ?? null,
      stack: lastError.stack ?? null,
    },
    stage: lastError.stage ?? "boot",
    occurredAt: lastError.occurredAt ?? "2025-01-01T00:00:00Z",
  } as BackendStatusResponse;
}

beforeEach(() => {
  // 复位 store 到干净初始态。
  act(() => {
    useBackendStore.getState().reset();
  });
  vi.mock("@/hooks/useBackend", () => ({
    useBackend: () => ({ restart: vi.fn().mockResolvedValue(undefined), isBusy: false }),
  }));
});

describe("BackendErrorBanner transportError 告警条 (M8)", () => {
  it("status=running 且 transportError 非空 → 显示非阻塞告警条（含连接中断+正在恢复）", () => {
    act(() => {
      useBackendStore.setState({
        status: "running",
        snapshot: null,
        transportError: "连接断开",
      });
    });

    render(<BackendErrorBanner onViewLogs={() => {}} />);

    const alert = screen.getByText(/后端连接中断/);
    expect(alert).toBeTruthy();
    expect(screen.getByText(/正在尝试恢复/)).toBeTruthy();
    // 告警原文应拼接 transportError 内容。
    expect(screen.getByText(/连接断开/)).toBeTruthy();
  });

  it("status=ready 且 transportError 非空 → 同样显示告警条（running/ready 同分支）", () => {
    act(() => {
      useBackendStore.setState({
        status: "ready",
        snapshot: null,
        transportError: "IPC 通道丢失",
      });
    });

    render(<BackendErrorBanner onViewLogs={() => {}} />);

    expect(screen.getByText(/后端连接中断/)).toBeTruthy();
    expect(screen.getByText(/IPC 通道丢失/)).toBeTruthy();
  });

  it("status=ready 且 transportError=null → 无告警条（返回 null）", () => {
    act(() => {
      useBackendStore.setState({
        status: "ready",
        snapshot: null,
        transportError: null,
      });
    });

    render(<BackendErrorBanner onViewLogs={() => {}} />);

    expect(screen.queryByText(/后端连接中断/)).toBeNull();
    expect(screen.queryByText(/正在尝试恢复/)).toBeNull();
  });

  it("status=stopped 且 transportError 非空 → 因不在 running/ready 分支，不显示 transport 告警条", () => {
    act(() => {
      useBackendStore.setState({
        status: "stopped",
        snapshot: null,
        transportError: "连接断开",
      });
    });

    render(<BackendErrorBanner onViewLogs={() => {}} />);

    expect(screen.queryByText(/后端连接中断/)).toBeNull();
  });

  it("status=failed 且 snapshot.lastError 存在 → 渲染结构化错误卡，且不与 transport 告警叠加", () => {
    act(() => {
      useBackendStore.setState({
        status: "failed",
        snapshot: makeSnapshot({ message: "后端启动失败", detail: "端口被占用" }),
        transportError: "连接断开", // 即便有 transportError，failed 卡也不应叠加告警条
      });
    });

    render(<BackendErrorBanner onViewLogs={() => {}} />);

    // 结构化错误卡出现。
    expect(screen.getByText("后端启动失败")).toBeTruthy();
    // failed 分支下不应出现 transport 告警条文案。
    expect(screen.queryByText(/正在尝试恢复/)).toBeNull();
    expect(screen.queryByText(/后端连接中断/)).toBeNull();
  });

  it("status=failed 但 snapshot.lastError 为空 → 返回 null（不渲染任何内容）", () => {
    act(() => {
      useBackendStore.setState({
        status: "failed",
        snapshot: { status: "failed", managed: false, pid: null, url: null, started_at: null, lastError: null, stage: "boot", occurredAt: "" } as BackendStatusResponse,
        transportError: null,
      });
    });

    const { container } = render(<BackendErrorBanner onViewLogs={() => {}} />);
    expect(container.querySelector("*")).toBeNull();
  });

  it("transportError 在 failed → running 状态切换时，告警条随 ready/running 出现", () => {
    act(() => {
      useBackendStore.setState({
        status: "failed",
        snapshot: makeSnapshot({ message: "后端启动失败" }),
        transportError: null,
      });
    });
    const { rerender } = render(<BackendErrorBanner onViewLogs={() => {}} />);
    expect(screen.getByText("后端启动失败")).toBeTruthy();

    // 后端重启成功进入 running，但 transport 仍抖动产错。
    act(() => {
      useBackendStore.setState({
        status: "running",
        snapshot: null,
        transportError: "重连中抖动",
      });
    });
    rerender(<BackendErrorBanner onViewLogs={() => {}} />);

    expect(screen.queryByText("后端启动失败")).toBeNull();
    expect(screen.getByText(/后端连接中断/)).toBeTruthy();
    expect(screen.getByText(/重连中抖动/)).toBeTruthy();
  });
});
