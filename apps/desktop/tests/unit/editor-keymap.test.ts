import { acceptCompletion } from "@codemirror/autocomplete";
import { cursorLineDown, insertNewlineAndIndent, simplifySelection } from "@codemirror/commands";
import type { KeyBinding } from "@codemirror/view";
import { describe, expect, it } from "vitest";

import type { HostPlatform } from "@/electron/shared/host-contract";
import { commandBindings, EDITING_BINDINGS, EDITOR_COMMAND_IDS, POPUP_BINDINGS } from "@/components/code-editor/editor-keymap";
import { codeMirrorKey } from "@/electron/shared/key-names";
import { resolveShortcutKeys, scopeAncestry, shortcutScopes, SHORTCUTS, type ShortcutId } from "@/lib/keyboard/shortcuts";

/**
 * 编辑器键位的守卫：功能键与登记表一一对应（登记了就绑上，绑上的都登记过），且不与留给 CodeMirror 的文字
 * 编辑键（标准键表、撤销重做、补全弹层、括号闭合）在任何平台上撞键——那些键不经登记表，撞了没人看得见。
 */
const PLATFORMS: readonly HostPlatform[] = ["darwin", "win32", "linux"];
const IDS = Object.keys(SHORTCUTS) as ShortcutId[];


/** CodeMirror 绑定在某平台上的具体键名（含 shift 变体），统一成「修饰键排序-主键」。 */
function bindingKeys(binding: KeyBinding, platform: HostPlatform): string[] {
  const key = platform === "darwin" ? binding.mac ?? binding.key : platform === "win32" ? binding.win ?? binding.key : binding.linux ?? binding.key;
  if (!key) return [];
  return [key, ...(binding.shift ? [`Shift-${key}`] : [])].map((name) => normalize(name, platform));
}

function normalize(name: string, platform: HostPlatform): string {
  const parts = name.split(/-(?!$)/);
  const main = parts.pop()!.toLowerCase();
  const modifiers = parts.map((part) => {
    const lower = part.toLowerCase();
    if (lower === "mod") return platform === "darwin" ? "meta" : "ctrl";
    if (lower === "cmd") return "meta";
    if (lower === "control") return "ctrl";
    return lower;
  });
  return [...modifiers.sort(), main].join("-");
}

describe("编辑器键位来自登记表", () => {
  it("代码编辑器范围的条目都绑上了命令，绑上的命令都在这个范围登记", () => {
    const registered = IDS.filter((id) => shortcutScopes(id).includes("codeEditor")).sort();
    expect([...EDITOR_COMMAND_IDS].sort()).toEqual(registered);
  });

  it.each(PLATFORMS)("%s：功能键不与文字编辑键撞键", (platform) => {
    const editing = new Set([...POPUP_BINDINGS, ...EDITING_BINDINGS].flatMap((binding) => bindingKeys(binding, platform)));
    const active = IDS.filter((id) => shortcutScopes(id).some((scope) => scopeAncestry("markdownEditor").has(scope)));
    const clashes = active.filter((id) => editing.has(normalize(codeMirrorKey(resolveShortcutKeys(id, platform)), platform)));
    expect(clashes).toEqual([]);
  });
});

// 目的：同一个键先给上层；潜在缺陷：补全弹层的方向键与回车被光标移动、换行抢走，Esc 没人接，只读视图吞键。
describe("编辑器按键分层", () => {
  const first = (bindings: readonly KeyBinding[], key: string) => bindings.find((binding) => binding.key === key)?.run;

  it("补全弹层的方向键与回车在独立的最高层，文字编辑层的同键命令只在弹层没开时轮到", () => {
    expect(first(POPUP_BINDINGS, "Enter")).toBe(acceptCompletion);
    expect(first(POPUP_BINDINGS, "ArrowDown")).not.toBe(cursorLineDown);
    expect(first(EDITING_BINDINGS, "ArrowDown")).toBe(cursorLineDown);
    expect(first(EDITING_BINDINGS, "Enter")).toBe(insertNewlineAndIndent);
  });

  it("Esc 在文字编辑层收拢多选区", () => {
    expect(first(EDITING_BINDINGS, "Escape")).toBe(simplifySelection);
  });

  it.each(PLATFORMS)("%s：功能键不强制阻止默认行为（只读视图里不执行的命令不吞键）", (platform) => {
    expect(commandBindings(platform).filter((binding) => binding.preventDefault)).toEqual([]);
  });
});
