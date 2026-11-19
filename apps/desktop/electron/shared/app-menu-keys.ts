import type { HostPlatform } from "./host-contract";

/**
 * 宿主应用菜单的条目与快捷键、以及系统占用的按键：宿主装菜单（`electron/main/app-menu.ts`）与渲染侧快捷键
 * 登记表的守卫测试共读这一份，菜单改了键，登记表的保留键跟着变，不再手抄。
 *
 * 菜单快捷键在整个窗口生效，焦点在哪个网页里都可能触发（各平台先给页面还是先给菜单不尽相同），应用快捷键
 * 与它同键时两边互相抢，所以登记表里任何作用范围都不能用这些键。键位写法与登记表相同（`mod` = macOS ⌘ /
 * 其他平台 Ctrl，键名是 `KeyboardEvent.code` 去前缀的小写），装菜单时经 `key-names.ts` 的 `electronAccelerator`
 * 换成 Electron 的写法。条目与键位沿用 Electron 各菜单角色的默认值，写明了就是有意的选择：例如 Windows 的
 * 「退出」没有快捷键（Alt+F4 由系统关窗口）；唯一不同的是开发期的「放大」用 ⌘= / Ctrl+=（不按 Shift，与
 * 浏览器一致），Electron 默认是 ⌘+。
 */

export type MenuKeyItem = { role: MenuItemRole; keys?: string } | { separator: true } | { label: string; submenu: MenuKeyItem[] };

/** 这里用到的 Electron 菜单角色（`MenuItemConstructorOptions["role"]` 的子集；shared 不依赖 electron 类型）。 */
export type MenuItemRole =
  | "about" | "services" | "hide" | "hideOthers" | "unhide" | "quit"
  | "undo" | "redo" | "cut" | "copy" | "paste" | "pasteAndMatchStyle" | "delete" | "selectAll"
  | "startSpeaking" | "stopSpeaking"
  | "reload" | "forceReload" | "toggleDevTools" | "resetZoom" | "zoomIn" | "zoomOut" | "togglefullscreen"
  | "minimize" | "zoom" | "front" | "close";

export type AppMenuKeys = {
  /** macOS 的应用菜单；其他平台是「文件」菜单。 */
  app: MenuKeyItem[];
  edit: MenuKeyItem[];
  /** 刷新、缩放、开发者工具：只在开发时装（理由见 `app-menu.ts`）。 */
  view: MenuKeyItem[];
  window: MenuKeyItem[];
};

const separator = { separator: true } as const;

export function appMenuKeys(platform: HostPlatform): AppMenuKeys {
  const mac = platform === "darwin";
  return {
    app: mac
      ? [
        { role: "about" }, separator, { role: "services" }, separator,
        { role: "hide", keys: "mod+h" }, { role: "hideOthers", keys: "mod+alt+h" }, { role: "unhide" }, separator,
        { role: "quit", keys: "mod+q" },
      ]
      : [{ role: "quit", keys: platform === "linux" ? "mod+q" : undefined }],
    edit: [
      { role: "undo", keys: "mod+z" },
      { role: "redo", keys: platform === "win32" ? "ctrl+y" : "mod+shift+z" },
      separator,
      { role: "cut", keys: "mod+x" },
      { role: "copy", keys: "mod+c" },
      { role: "paste", keys: "mod+v" },
      ...(mac ? [{ role: "pasteAndMatchStyle", keys: "mod+alt+shift+v" } as const] : []),
      { role: "delete" },
      ...(mac ? [] : [separator]),
      { role: "selectAll", keys: "mod+a" },
      ...(mac ? [separator, { label: "Speech", submenu: [{ role: "startSpeaking" }, { role: "stopSpeaking" }] } satisfies MenuKeyItem] : []),
    ],
    view: [
      { role: "reload", keys: "mod+r" },
      { role: "forceReload", keys: "mod+shift+r" },
      { role: "toggleDevTools", keys: mac ? "mod+alt+i" : "ctrl+shift+i" },
      separator,
      { role: "resetZoom", keys: "mod+0" },
      { role: "zoomIn", keys: "mod+equal" },
      { role: "zoomOut", keys: "mod+minus" },
      separator,
      { role: "togglefullscreen", keys: mac ? "ctrl+meta+f" : "f11" },
    ],
    window: mac
      ? [{ role: "minimize", keys: "mod+m" }, { role: "zoom" }, separator, { role: "front" }]
      : [{ role: "minimize", keys: "mod+m" }, { role: "close", keys: "mod+w" }],
  };
}

/**
 * 系统与桌面环境占用、应用拿不到或不该抢的按键（菜单之外的）。Windows 与 Linux 的菜单栏标题带助记键
 * （File / Edit / View / Window），Alt+首字母会打开菜单。
 */
const SYSTEM_KEYS: Record<HostPlatform, readonly string[]> = {
  darwin: ["meta+w", "meta+tab", "meta+space", "ctrl+space", "meta+backquote", "meta+shift+3", "meta+shift+4", "meta+shift+5",
    "ctrl+arrowleft", "ctrl+arrowright", "ctrl+arrowup", "ctrl+arrowdown"],
  win32: ["alt+f4", "alt+tab", "ctrl+escape", "alt+f", "alt+e", "alt+v", "alt+w"],
  linux: ["alt+f4", "alt+tab", "ctrl+alt+t", "ctrl+alt+delete", "ctrl+alt+arrowleft", "ctrl+alt+arrowright",
    "ctrl+alt+arrowup", "ctrl+alt+arrowdown", "alt+f", "alt+e", "alt+v", "alt+w"],
};

function menuItemKeys(items: MenuKeyItem[]): string[] {
  return items.flatMap((item) => "submenu" in item ? menuItemKeys(item.submenu) : "role" in item && item.keys ? [item.keys] : []);
}

/**
 * 应用快捷键不能用的键（键位写法，含 `mod`）：开发期菜单也算，开发时同样不能冲突。编辑菜单不在其中：
 * 复制、粘贴、全选是文字编辑键，先交给页面处理，控件可以有意接管（终端的复制、模型选择器的全选），见 `editingKeys`。
 */
export function reservedKeys(platform: HostPlatform): string[] {
  const menu = appMenuKeys(platform);
  return [...menuItemKeys([...menu.app, ...menu.view, ...menu.window]), ...SYSTEM_KEYS[platform]];
}

/** 文字编辑键（编辑菜单的快捷键）：全局快捷键不能占，控件内可以有意接管。 */
export function editingKeys(platform: HostPlatform): string[] {
  return menuItemKeys(appMenuKeys(platform).edit);
}
