/** 局部渲染隔离层之间共享的范围、诊断元数据和 fallback 契约。 */

/** 需要独立捕获异常的前端展示范围。 */
export type RenderIsolationScope =
  | "message"
  | "part"
  | "markdown"
  | "tool"
  | "attachment";

/** 只允许记录稳定标识和展示类别，不承载正文、凭据或工具原始结果。 */
export type RenderIsolationMetadata = Record<string, unknown>;

/** fallback 接收的受控错误信息和当前局部重试操作。 */
export type RenderFallbackContext = {
  error: Error;
  onRetry: () => void;
};

/** 静态 fallback 或根据错误上下文生成的 fallback。 */
export type RenderFallback =
  | React.ReactNode
  | ((context: RenderFallbackContext) => React.ReactNode);
