import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";

import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";
import type {
  RenderFallback,
  RenderIsolationMetadata,
  RenderIsolationScope,
} from "./render-error-types";

export type { RenderFallback, RenderFallbackContext, RenderIsolationMetadata, RenderIsolationScope } from "./render-error-types";

type RenderIsolationBoundaryProps = {
  children: ReactNode;
  scope: RenderIsolationScope;
  resetKey: string;
  fallback: RenderFallback;
  metadata?: RenderIsolationMetadata;
  onRetry?: () => void;
};

type RenderIsolationBoundaryState = {
  error: Error | null;
  resetKey: string;
};

/**
 * 隔离单个前端渲染单元的异常，不改变任何 Assistant、transport 或业务事实。
 *
 * `resetKey` 标识被保护的渲染单元；身份变化会清除旧错误，用户点击重试只会
 * 重新挂载当前子树。渲染异常会通过 frontendLog 记录结构化诊断信息，但日志
 * 失败不会阻断 fallback 展示，也不会重新执行 Agent 或工具。
 */
export class RenderIsolationBoundary extends Component<
  RenderIsolationBoundaryProps,
  RenderIsolationBoundaryState
> {
  state: RenderIsolationBoundaryState;

  constructor(props: RenderIsolationBoundaryProps) {
    super(props);
    this.state = { error: null, resetKey: props.resetKey };
  }

  static getDerivedStateFromProps(
    props: RenderIsolationBoundaryProps,
    state: RenderIsolationBoundaryState,
  ): Partial<RenderIsolationBoundaryState> | null {
    if (props.resetKey === state.resetKey) return null;
    return { error: null, resetKey: props.resetKey };
  }

  static getDerivedStateFromError(error: Error): Partial<RenderIsolationBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    const traceId = newTraceId();
    void frontendLog("ERROR", "isolated_render_failed", "局部前端渲染失败，已降级显示", {
      traceId,
      data: {
        scope: this.props.scope,
        ...(this.props.metadata ?? {}),
        componentStack: info.componentStack ?? "",
      },
      error,
    }).catch(() => undefined);
  }

  private handleRetry = (): void => {
    this.setState({ error: null });
    this.props.onRetry?.();
  };

  render(): ReactNode {
    if (this.state.error === null) return this.props.children;

    const fallback = this.props.fallback;
    if (typeof fallback === "function") {
      return fallback({ error: this.state.error, onRetry: this.handleRetry });
    }
    return fallback;
  }
}
