import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";

import { frontendLog } from "@/lib/logging/frontend-log";

type RuntimeErrorBoundaryProps = {
  taskId: number;
  children: ReactNode;
  onRetry: () => void;
};

type RuntimeErrorBoundaryState = {
  error: Error | null;
};

/** Isolate render failures from the transport session and expose a retry action. */
export class AssistantRuntimeErrorBoundary extends Component<
  RuntimeErrorBoundaryProps,
  RuntimeErrorBoundaryState
> {
  state: RuntimeErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RuntimeErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    void frontendLog("ERROR", "assistant_runtime_render_failed", "Assistant 对话界面渲染失败", {
      data: { taskId: this.props.taskId, componentStack: info.componentStack ?? "" },
      error,
    });
  }

  private handleRetry = (): void => {
    this.setState({ error: null });
    this.props.onRetry();
  };

  render(): ReactNode {
    if (this.state.error === null) return this.props.children;
    return (
      <section className="flex h-full min-h-0 flex-col items-center justify-center gap-3 p-6 text-center">
        <h2 className="text-sm font-medium">对话界面渲染失败</h2>
        <p className="text-muted-foreground max-w-md text-xs">对话数据仍保存在本机，可以重试恢复界面。</p>
        <button type="button" className="text-sm underline underline-offset-4" onClick={this.handleRetry}>重试</button>
      </section>
    );
  }
}
