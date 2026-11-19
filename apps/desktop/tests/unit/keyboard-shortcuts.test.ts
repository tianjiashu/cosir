import { describe, expect, it } from "vitest";

import type { HostPlatform } from "@/electron/shared/host-contract";
import { appMenuKeys, editingKeys, reservedKeys, type MenuKeyItem } from "@/electron/shared/app-menu-keys";
import { codeMirrorKey, electronAccelerator } from "@/electron/shared/key-names";
import {
  formatKeys,
  matchesShortcut,
  resolveShortcutKeys,
  scopeAncestry,
  shortcutScopes,
  shortcutLabel,
  SHORTCUTS,
  type ShortcutId,
} from "@/lib/keyboard/shortcuts";

const PLATFORMS: readonly HostPlatform[] = ["darwin", "win32", "linux"];
const IDS = Object.keys(SHORTCUTS) as ShortcutId[];

/** 换成具体键位（mod → meta / ctrl），与 `resolveShortcutKeys` 一致。 */
function concrete(keys: string, platform: HostPlatform): string {
  return keys.split("+").map((part) => (part === "mod" ? (platform === "darwin" ? "meta" : "ctrl") : part)).join("+");
}

/** macOS 上 Option（加或不加 Shift）配可打字的键会输入字符（Option+A → å），不能当快捷键。 */
const TYPING_KEY = /^([a-z0-9]|backquote|minus|equal|bracketleft|bracketright|backslash|semicolon|quote|comma|period|slash)$/;

function normalize(combo: string): string {
  const parts = combo.split("+");
  const key = parts.pop()!;
  return [...parts.sort(), key].join("+");
}

function overrides(id: ShortcutId): readonly string[] {
  const definition = SHORTCUTS[id];
  return "overrides" in definition ? definition.overrides : [];
}

function event(code: string, modifiers: { meta?: boolean; ctrl?: boolean; alt?: boolean; shift?: boolean } = {}, altGraph = false) {
  return {
    code,
    metaKey: modifiers.meta ?? false,
    ctrlKey: modifiers.ctrl ?? false,
    altKey: modifiers.alt ?? false,
    shiftKey: modifiers.shift ?? false,
    getModifierState: (key: string) => key === "AltGraph" && altGraph,
  };
}

describe.each(PLATFORMS)("快捷键登记表（%s）", (platform) => {
  // 写了几个范围的条目按范围展开，各自参与检查（同一条目的几份之间不算冲突）。
  const combos = IDS.flatMap((id) => shortcutScopes(id).map((scope) => ({ id, combo: normalize(resolveShortcutKeys(id, platform)), scope })));

  it("同一作用范围链上不重复，除非内层显式声明覆盖", () => {
    const conflicts: string[] = [];
    combos.forEach((a, index) => {
      for (const b of combos.slice(index + 1)) {
        if (a.combo !== b.combo || a.id === b.id) continue;
        // 兄弟范围不会同时获得焦点；同一范围内同键一定冲突；嵌套时内层须声明覆盖外层。
        const [inner, outer] = scopeAncestry(a.scope).has(b.scope) ? [a, b] : [b, a];
        if (!scopeAncestry(inner.scope).has(outer.scope)) continue;
        if (inner.scope === outer.scope || !overrides(inner.id).includes(outer.id)) conflicts.push(`${inner.id} ↔ ${outer.id}（${a.combo}）`);
      }
    });
    expect(conflicts).toEqual([]);
  });

  it("覆盖声明都指向同键的外层条目（声明不会过期）", () => {
    for (const { id, combo, scope } of combos) {
      for (const target of overrides(id)) {
        const outer = combos.find((candidate) => candidate.id === target && scopeAncestry(scope).has(candidate.scope));
        expect(outer, `${id} 覆盖了不存在的 ${target}`).toBeDefined();
        expect(outer!.scope !== scope && scopeAncestry(scope).has(outer!.scope), `${target} 不在 ${id} 的外层`).toBe(true);
        expect(outer!.combo, `${id} 与 ${target} 在 ${platform} 上已不同键`).toBe(combo);
      }
    }
  });

  it("不占用系统与宿主菜单的保留键", () => {
    const reserved = new Set(reservedKeys(platform).map((keys) => normalize(concrete(keys, platform))));
    expect(combos.filter(({ combo }) => reserved.has(combo)).map(({ id, combo }) => `${id}（${combo}）`)).toEqual([]);
  });

  it("全局快捷键不占用文字编辑键", () => {
    const editing = new Set(editingKeys(platform).map((keys) => normalize(concrete(keys, platform))));
    expect(combos.filter(({ scope, combo }) => scope === "global" && editing.has(combo)).map(({ id }) => id)).toEqual([]);
  });

  if (platform === "darwin") {
    it("macOS 不用 Option 配可打字的键：那是在输入字符", () => {
      expect(combos.filter(({ combo }) => {
        const parts = combo.split("+");
        return parts.includes("alt") && !parts.includes("meta") && !parts.includes("ctrl") && TYPING_KEY.test(parts.at(-1)!);
      }).map(({ id }) => id)).toEqual([]);
    });
  } else {
    it("Windows 与 Linux 不用 Ctrl+Alt 组合：Windows 上它等同 AltGr，Linux 桌面环境大量占用", () => {
      expect(combos.filter(({ combo }) => combo.includes("alt") && combo.includes("ctrl")).map(({ id }) => id)).toEqual([]);
    });
  }
});

// 目的：键位只经一份词汇表转换；潜在缺陷：某个键名三处写法里有一处没收录，原样放行后在编辑器或菜单里永远不触发。
describe("按键词汇", () => {
  const menuKeys = (items: MenuKeyItem[]): string[] =>
    items.flatMap((item) => "submenu" in item ? menuKeys(item.submenu) : "role" in item && item.keys ? [item.keys] : []);

  it.each(PLATFORMS)("%s：登记表与菜单的每个键都能转成展示、CodeMirror 与 Electron 写法", (platform) => {
    for (const id of IDS) {
      const keys = resolveShortcutKeys(id, platform);
      expect(() => [shortcutLabel(id, platform), codeMirrorKey(keys), electronAccelerator(keys)], id).not.toThrow();
    }
    const menu = appMenuKeys(platform);
    for (const keys of menuKeys([...menu.app, ...menu.edit, ...menu.view, ...menu.window])) expect(() => electronAccelerator(keys), keys).not.toThrow();
  });

  it("不认识的键直接报错，不原样放行", () => {
    expect(() => codeMirrorKey("ctrl+nosuchkey")).toThrow(/未登记的按键部件/);
    expect(() => electronAccelerator("mod+nosuchkey")).toThrow(/未登记的按键部件/);
    expect(() => formatKeys("mod+nosuchkey", "darwin")).toThrow(/未登记的按键部件/);
  });
});

describe("按键匹配", () => {
  it("macOS 只认 ⌘，Windows 只认 Ctrl", () => {
    expect(matchesShortcut(event("KeyB", { meta: true }), "markdownBold", "darwin")).toBe(true);
    expect(matchesShortcut(event("KeyB", { ctrl: true }), "markdownBold", "darwin")).toBe(false);
    expect(matchesShortcut(event("KeyB", { ctrl: true }), "markdownBold", "win32")).toBe(true);
    expect(matchesShortcut(event("KeyB", { meta: true }), "markdownBold", "win32")).toBe(false);
  });

  it("修饰键精确匹配，主键按物理键位", () => {
    expect(matchesShortcut(event("KeyB", { meta: true, shift: true }), "markdownBold", "darwin")).toBe(false);
    expect(matchesShortcut(event("Digit8", { meta: true, shift: true }), "markdownBulletList", "darwin")).toBe(true);
    expect(matchesShortcut(event("Backquote", { ctrl: true }), "toggleTerminal", "darwin")).toBe(true);
    expect(matchesShortcut(event("Enter", { ctrl: true }), "questionSubmit", "win32")).toBe(true);
  });

  it("AltGr 输入的字符不算快捷键", () => {
    expect(matchesShortcut(event("KeyB", { ctrl: true, alt: true }, true), "markdownBold", "win32")).toBe(false);
  });

  it("分平台键位各取一份", () => {
    expect(resolveShortcutKeys("toggleWorkbench", "darwin")).toBe("meta+alt+b");
    expect(resolveShortcutKeys("toggleWorkbench", "win32")).toBe("ctrl+shift+b");
    expect(matchesShortcut(event("Period", { ctrl: true, shift: true }), "toggleConversationAside", "win32")).toBe(true);
  });
});

describe("展示文案", () => {
  it("按平台习惯展示", () => {
    expect(shortcutLabel("newConversation", "darwin")).toBe("⌘N");
    expect(shortcutLabel("newConversation", "win32")).toBe("Ctrl+N");
    expect(shortcutLabel("toggleConversationAside", "win32")).toBe("Ctrl+Shift+.");
    expect(shortcutLabel("toggleTerminal", "darwin")).toBe("⌃`");
    expect(formatKeys("shift+enter", "win32")).toBe("Shift+↵");
    expect(shortcutLabel("newConversation", "linux")).toBe("Ctrl+N");
    expect(formatKeys("meta+l", "linux")).toBe("Super+L");
  });
});
