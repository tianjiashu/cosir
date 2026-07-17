/**
 * React 错误边界组件。
 *
 * 捕获子组件树中的渲染错误，避免整个应用白屏，
 * 并在界面上展示可排查的错误摘要与堆栈。
 *
 * @module components/ErrorBoundary
 */

import { Component, type ReactNode } from "react";
import { logError } from "@/lib/logger";

interface ErrorBoundaryProps {
  /** 子组件树。 */
  children: ReactNode;
}

interface ErrorBoundaryState {
  /** 是否已捕获错误。 */
  hasError: boolean;
  /** 捕获到的错误对象。 */
  error: Error | null;
}

/**
 * 错误边界组件。
 *
 * 用于包裹 App 根组件，捕获子树中的同步渲染错误。
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    logError("React 渲染错误", error, { module: "ErrorBoundary", componentStack: errorInfo.componentStack });
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex h-screen w-screen flex-col items-center justify-center gap-4 bg-background p-8 text-foreground">
          <h1 className="text-xl font-semibold">Coding Agent 启动失败</h1>
          <p className="max-w-md text-center text-sm text-muted-foreground">
            应用渲染时发生错误。请检查 Tauri WebView 控制台或日志文件后重启。
          </p>
          {this.state.error ? (
            <pre className="max-h-96 max-w-2xl overflow-auto rounded bg-muted p-4 text-xs">
              {this.state.error.message}
              {"\n"}
              {this.state.error.stack}
            </pre>
          ) : null}
        </div>
      );
    }

    return this.props.children;
  }
}
