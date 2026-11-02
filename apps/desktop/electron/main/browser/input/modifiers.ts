import type { Modifier } from "../params";

/**
 * CDP 输入事件（键盘与鼠标共用）的修饰键位。修饰键只保证页面自己处理的快捷键（如 Cmd+Enter 发送、Shift 点击扩选）：
 * macOS 上 CDP 合成的编辑快捷键（全选、撤销等）还需另传编辑命令才会生效，这里不提供，编辑文字用 `type`。
 */
const MODIFIER_BITS = { Alt: 1, Control: 2, Meta: 4, Shift: 8 } as const;

export function isSupportedModifier(modifier: string): boolean {
  return modifier === "ControlOrMeta" || Object.hasOwn(MODIFIER_BITS, modifier);
}

export function modifierBits(modifiers: readonly Modifier[]): number {
  return modifiers.reduce((sum, item) => sum | MODIFIER_BITS[resolve(item)], 0);
}

/** `ControlOrMeta` 在 macOS 上是 Meta、其他平台是 Control。 */
function resolve(modifier: Modifier): keyof typeof MODIFIER_BITS {
  if (modifier !== "ControlOrMeta") return modifier;
  return process.platform === "darwin" ? "Meta" : "Control";
}
