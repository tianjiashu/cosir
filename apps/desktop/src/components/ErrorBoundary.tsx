/**
 * 应用级错误边界（基于 react-error-boundary）。
 *
 * 用函数式 `<ErrorBoundary>` 替换原 Class Component，并提供：
 * - 渲染期异常捕获（子组件抛错时不白屏，展示可重试的兜底 UI）；
 * - 用户点击「重试」经 `resetErrorBoundary` 重置错误状态；
 * - 捕获的异常经统一日志出口 `logError` 记录，保证可排查（不静默吞错）。
 *
 * 替换原 Class Component 的原因：自研 Class Component 无重试能力，且函数式
 * 写法与项目整体风格一致、复用社区维护的成熟库（react-error-boundary）。
 *
 * @module components/ErrorBoundary
 */

import { ErrorBoundary as ReactErrorBoundary } from "react-error-boundary";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { logError, logInfo } from "@/lib/logger";

/** 错误兜底 UI 的属性（react-error-boundary 回调签名）。 */
interface FallbackProps {
  /** 捕获到的错误对象。 */
  error: Error;
  /** 重置错误边界、重新渲染子树的回调。 */
  resetErrorBoundary: () => void;
}

/**
 * 错误兜底展示组件。
 *
 * 目的：
 *   在渲染异常时展示可读的错误信息 + 重试按钮，避免整个应用白屏；
 *   同时不暴露内部堆栈给用户。
 *
 * 参数：
 *   error - 捕获到的错误对象。
 *   resetErrorBoundary - 重置错误边界的回调。
 *
 * 返回：
 *   错误兜底的 React 元素。
 */
function ErrorFallback({ error, resetErrorBoundary }: FallbackProps) {
  return (
    <div className="flex min-h-[200px] flex-col items-center justify-center gap-3 p-6 text-center">
      <AlertTriangle className="h-8 w-8 text-destructive" />
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">界面渲染出现异常</p>
        <p className="max-w-md text-xs text-muted-foreground">{error.message || "未知错误"}</p>
      </div>
      <Button variant="outline" size="sm" onClick={resetErrorBoundary} className="gap-1.5">
        <RotateCcw className="h-3.5 w-3.5" />
        重试
      </Button>
    </div>
  );
}

/**
 * 应用级错误边界组件（函数式，封装 react-error-boundary）。
 *
 * 目的：
 *   包裹根组件，捕获子树渲染异常并展示兜底 UI，异常经统一日志出口记录。
 *
 * 参数：
 *   children - 受保护的子子树（通常为 `<App />`）。
 *
 * 返回：
 *   带错误边界保护的 React 元素。
 */
export function ErrorBoundary({ children }: { children: React.ReactNode }) {
  return (
    <ReactErrorBoundary
      FallbackComponent={ErrorFallback}
      onError={(error) => {
        logError("渲染子树发生异常", error, { module: "ErrorBoundary" });
      }}
      onReset={() => {
        // 重置是正常用户操作（非错误路径），用 INFO 级别记录便于复盘，避免污染错误日志。
        logInfo("错误边界已重置（用户重试）", { module: "ErrorBoundary" });
      }}
    >
      {children}
    </ReactErrorBoundary>
  );
}
