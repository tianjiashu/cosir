import {
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";

import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

type RootErrorBoundaryProps = {
  children: ReactNode;
};

type RootErrorBoundaryState = {
  error: Error | null;
};

/** Keep a root render failure visible and recoverable instead of leaving a blank WebView. */
export class RootErrorBoundary extends Component<
  RootErrorBoundaryProps,
  RootErrorBoundaryState
> {
  state: RootErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RootErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    const traceId = newTraceId();
    void frontendLog("ERROR", "root_render_failed", "桌面应用根界面渲染失败", {
      traceId,
      data: { componentStack: info.componentStack ?? "" },
      error,
    }).catch(() => undefined);
  }

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (this.state.error === null) return this.props.children;

    return (
      <main className="flex min-h-dvh items-center justify-center p-6">
        <section className="max-w-md space-y-4 text-center">
          <h1 className="text-lg font-semibold">Cosir 界面暂时不可用</h1>
          <p className="text-muted-foreground text-sm">
            对话数据仍保存在本机，可以重新加载界面恢复。
          </p>
          <button type="button" className="underline" onClick={this.handleReload}>
            重新加载
          </button>
        </section>
      </main>
    );
  }
}
