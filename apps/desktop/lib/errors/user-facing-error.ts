/**
 * 前端主动抛出、文案可以原样展示给用户的错误（表单校验、业务前置条件等）。
 *
 * 只有这种错误和后端给出可读原因的 HttpError 会把 message 透给界面；
 * 其余 Error（库、网络、运行时异常）的 message 一律不展示，由 userErrorMessage 收敛为兜底文案。
 * 写给开发者的不变量断言（如「组件必须位于某 Provider 内」）继续用普通 Error。
 */
export class UserFacingError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UserFacingError";
  }
}
