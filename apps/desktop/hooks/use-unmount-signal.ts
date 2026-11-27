import { useCallback, useEffect, useRef } from "react";

/**
 * 组件卸载时中止的信号：交给「属于这个组件」的异步流程（如编辑器里保存前的探活），组件没了流程就不再往下走。
 *
 * 只用来中止没有副作用的请求，并在请求之后、写入之前检查 `aborted`；已经发出的写入不要挂这个信号——
 * 中止的是客户端等待，不是后端执行，挂上只会让前端不知道写入结果。
 *
 * 返回取信号的函数而不是信号本身：StrictMode 下效果会先卸载再挂载一次，卸载时中止的那个控制器在重新挂载时换新。
 */
export function useUnmountSignal(): () => AbortSignal {
  const controller = useRef(new AbortController());
  useEffect(() => {
    if (controller.current.signal.aborted) controller.current = new AbortController();
    const current = controller.current;
    return () => current.abort();
  }, []);
  return useCallback(() => controller.current.signal, []);
}
