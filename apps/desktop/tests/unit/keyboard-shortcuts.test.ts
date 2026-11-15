import { describe, expect, it } from "vitest";

import type { HostPlatform } from "@/electron/shared/host-contract";
import {
  formatKeys,
  matchesShortcut,
  resolveShortcutKeys,
  SCOPE_PARENT,
  shortcutLabel,
  SHORTCUTS,
  type ShortcutId,
  type ShortcutScope,
} from "@/lib/keyboard/shortcuts";

const PLATFORMS: readonly HostPlatform[] = ["darwin", "win32"];
const IDS = Object.keys(SHORTCUTS) as ShortcutId[];

/**
 * 保留键：系统、宿主菜单（`electron/main/app-menu.ts` 的菜单角色，含开发期视图菜单）占用的组合，
 * 任何作用范围都不能用。按具体键位写（mod 已换成 meta / ctrl）。
 */
const RESERVED: Record<HostPlatform, readonly string[]> = {
  darwin: [
    "meta+q", "meta+w", "meta+h", "meta+m", "meta+backquote", "meta+tab", "meta+space",
    "meta+r", "meta+shift+r", "meta+alt+i", "meta+0", "meta+equal", "meta+minus", "ctrl+meta+f",
  ],
  win32: ["alt+f4", "ctrl+r", "ctrl+shift+r", "ctrl+shift+i", "ctrl+0", "ctrl+equal", "ctrl+minus", "f11"],
  linux: [],
};

/** 文字编辑的标准键：全局快捷键不能占（控件内可以有意覆盖，如模型选择器的全选）。 */
const EDITING: Record<HostPlatform, readonly string[]> = {
  darwin: ["meta+c", "meta+v", "meta+x", "meta+z", "meta+shift+z", "meta+a"],
  win32: ["ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+a"],
  linux: [],
};

function normalize(combo: string): string {
  const parts = combo.split("+");
  const key = parts.pop()!;
  return [...parts.sort(), key].join("+");
}

function scopeChain(scope: ShortcutScope): ShortcutScope[] {
  return scope === "global" ? ["global"] : [scope, ...scopeChain(SCOPE_PARENT[scope])];
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
  const combos = IDS.map((id) => ({ id, combo: normalize(resolveShortcutKeys(id, platform)), scope: SHORTCUTS[id].scope }));

  it("同一作用范围链上不重复，除非内层显式声明覆盖", () => {
    const conflicts: string[] = [];
    combos.forEach((a, index) => {
      for (const b of combos.slice(index + 1)) {
        if (a.combo !== b.combo) continue;
        // 兄弟范围不会同时获得焦点；同一范围内同键一定冲突；嵌套时内层须声明覆盖外层。
        const [inner, outer] = scopeChain(a.scope).includes(b.scope) ? [a, b] : [b, a];
        if (!scopeChain(inner.scope).includes(outer.scope)) continue;
        if (inner.scope === outer.scope || !overrides(inner.id).includes(outer.id)) conflicts.push(`${inner.id} ↔ ${outer.id}（${a.combo}）`);
      }
    });
    expect(conflicts).toEqual([]);
  });

  it("覆盖声明都指向同键的外层条目（声明不会过期）", () => {
    for (const { id, combo, scope } of combos) {
      for (const target of overrides(id)) {
        const outer = combos.find((candidate) => candidate.id === target);
        expect(outer, `${id} 覆盖了不存在的 ${target}`).toBeDefined();
        expect(scopeChain(scope).slice(1)).toContain(outer!.scope);
        expect(outer!.combo, `${id} 与 ${target} 在 ${platform} 上已不同键`).toBe(combo);
      }
    }
  });

  it("不占用系统与宿主菜单的保留键", () => {
    const reserved = new Set(RESERVED[platform].map(normalize));
    expect(combos.filter(({ combo }) => reserved.has(combo)).map(({ id, combo }) => `${id}（${combo}）`)).toEqual([]);
  });

  it("全局快捷键不占用文字编辑键", () => {
    const editing = new Set(EDITING[platform].map(normalize));
    expect(combos.filter(({ scope, combo }) => scope === "global" && editing.has(combo)).map(({ id }) => id)).toEqual([]);
  });

  if (platform === "win32") {
    it("Windows 不用 Ctrl+Alt 组合：它等同 AltGr，欧洲键盘布局靠它输入字符", () => {
      expect(combos.filter(({ combo }) => combo.includes("alt") && combo.includes("ctrl")).map(({ id }) => id)).toEqual([]);
    });
  }
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
  });
});
