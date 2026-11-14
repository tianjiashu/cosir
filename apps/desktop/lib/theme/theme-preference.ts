import { create } from "zustand";
import { persist } from "zustand/middleware";
import { useShallow } from "zustand/react/shallow";

import { desktopHost } from "@/lib/desktop-host";
import { frontendLog } from "@/lib/logging/frontend-log";
import { preferenceStorage } from "@/lib/storage/preference-storage";

export type ThemePreference = "light" | "dark" | "system";
type ResolvedTheme = Exclude<ThemePreference, "system">;

export type ThemeState = {
  preference: ThemePreference;
  resolvedTheme: ResolvedTheme;
};

/**
 * 外观偏好的存储 key。`index.html` 的首屏脚本在 React 之前按同一个 key 读 persist 的 JSON
 * （`{ state: { preference } }`）先打上深色类，避免首帧闪白；这是唯一在本模块之外读偏好的地方。
 */
const STORAGE_KEY = "cosir:theme";
const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";
const THEME_COLORS: Record<ResolvedTheme, string> = {
  light: "#ffffff",
  dark: "#0a0a0a",
};

function isThemePreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference !== "system") return preference;
  return window.matchMedia(DARK_SCHEME_QUERY).matches ? "dark" : "light";
}

/** 读入时收敛：不认识的值回到跟随系统。 */
export function normalizeThemePreference(raw: unknown): { preference: ThemePreference } {
  const preference = typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>).preference : undefined;
  return { preference: isThemePreference(preference) ? preference : "system" };
}

/** 只持久化偏好；实际深浅由偏好与系统外观现算（`resolvedTheme`），跟随系统时随系统变化。 */
const useThemeStore = create<ThemeState>()(persist(
  () => ({ preference: "system" as ThemePreference, resolvedTheme: "light" as ResolvedTheme }),
  {
    name: STORAGE_KEY,
    storage: preferenceStorage<{ preference: ThemePreference }>(),
    partialize: ({ preference }) => ({ preference }),
    merge: (persisted, current) => {
      const { preference } = normalizeThemePreference(persisted);
      return { ...current, preference, resolvedTheme: resolveTheme(preference) };
    },
  },
));

function applyTheme({ preference, resolvedTheme }: ThemeState): void {
  document.documentElement.classList.toggle("dark", resolvedTheme === "dark");
  document
    .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_COLORS[resolvedTheme]);
  // 原生外观（系统对话框、菜单、Windows 窗口按钮）跟随同一偏好；同步失败只影响原生部分，不打断主题切换。
  desktopHost?.window.setThemeSource(preference).catch((error: unknown) => {
    void frontendLog("WARNING", "native_theme_sync_failed", "同步原生外观失败", { error });
  });
}

/**
 * 在 React 挂载前接管首屏脚本：应用已读入的偏好，此后每次变化都重新应用；只有跟随系统时监听操作系统外观。
 */
export function installThemePreferenceSync(): () => void {
  applyTheme(useThemeStore.getState());
  const unsubscribe = useThemeStore.subscribe((next, previous) => {
    if (next.preference !== previous.preference || next.resolvedTheme !== previous.resolvedTheme) applyTheme(next);
  });
  const systemQuery = window.matchMedia(DARK_SCHEME_QUERY);
  const followSystem = () => {
    const { preference } = useThemeStore.getState();
    if (preference === "system") useThemeStore.setState({ resolvedTheme: resolveTheme("system") });
  };
  systemQuery.addEventListener("change", followSystem);
  return () => {
    unsubscribe();
    systemQuery.removeEventListener("change", followSystem);
  };
}

/** 用户选择立即作用于界面；写入本机存储失败由存储边界提示，只影响下次启动。 */
export function setThemePreference(preference: ThemePreference): void {
  useThemeStore.setState({ preference, resolvedTheme: resolveTheme(preference) });
}

export function useThemeState(): ThemeState {
  return useThemeStore(useShallow(({ preference, resolvedTheme }) => ({ preference, resolvedTheme })));
}
