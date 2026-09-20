/**
 * 决定一次性终端 renderer 如何把新的 Transport 输出应用到终端实例。
 *
 * 该模块只比较已经由 backend 投影的原始终端文本和输出序号，不解释 ANSI/VT
 * 控制序列；控制序列的状态机由 TerminalViewport 持有的 xterm 实例负责。
 */

export type TerminalOutputReconcileAction =
  | { kind: "append"; text: string }
  | { kind: "reset"; text: string }
  | { kind: "ignore" };

export type TerminalOutputReconcileInput = {
  previousOutput: string;
  previousSeq?: number;
  nextOutput: string;
  nextSeq?: number;
};

/**
 * 将新的快照输出转换为 xterm 的最小写入操作。
 *
 * 正常流式快照是旧输出的前缀，此时只追加后缀；相同文本或严格过期序号不重复写入。
 * 同序号但内容变化被视为一次重同步，而不是追加，避免把异常快照叠加到旧屏幕上。
 * 如果终态输出替换了实时截断输出，或者 snapshot 恢复后不再保持前缀关系，
 * 则要求调用方 reset 后重放完整文本。
 */
export function reconcileTerminalOutput({
  previousOutput,
  previousSeq,
  nextOutput,
  nextSeq,
}: TerminalOutputReconcileInput): TerminalOutputReconcileAction {
  if (nextSeq !== undefined && previousSeq !== undefined && nextSeq < previousSeq) {
    return { kind: "ignore" };
  }

  if (nextOutput === previousOutput) {
    return { kind: "ignore" };
  }

  if (nextSeq !== undefined && previousSeq !== undefined && nextSeq === previousSeq) {
    return { kind: "reset", text: nextOutput };
  }

  if (nextOutput.startsWith(previousOutput)) {
    return { kind: "append", text: nextOutput.slice(previousOutput.length) };
  }

  return { kind: "reset", text: nextOutput };
}
