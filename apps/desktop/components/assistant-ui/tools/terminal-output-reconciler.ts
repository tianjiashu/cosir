/**
 * 决定一次性终端 renderer 如何把新的 Transport 输出应用到终端实例。
 *
 * 该模块只比较已经由 backend 投影的原始终端文本，不解释 ANSI/VT 控制序列；
 * 控制序列的状态机由 TerminalViewport 持有的 xterm 实例负责。
 */

export type TerminalOutputReconcileAction =
  | { kind: "append"; text: string }
  | { kind: "reset"; text: string }
  | { kind: "ignore" };

/**
 * 将新的快照输出转换为 xterm 的最小写入操作。
 *
 * 正常流式快照是旧输出的前缀，此时只追加后缀；相同文本不重复写入。
 * 如果终态输出替换了实时截断输出，或者 snapshot 恢复后不再保持前缀关系，
 * 则要求调用方 reset 后重放完整文本。
 */
export function reconcileTerminalOutput(previousOutput: string, nextOutput: string): TerminalOutputReconcileAction {
  if (nextOutput === previousOutput) return { kind: "ignore" };
  if (nextOutput.startsWith(previousOutput)) return { kind: "append", text: nextOutput.slice(previousOutput.length) };
  return { kind: "reset", text: nextOutput };
}
