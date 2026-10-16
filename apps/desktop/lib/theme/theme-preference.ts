import { useSyncExternalStore } from "react";

import { frontendLog } from "@/lib/logging/frontend-log";

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = Exclude<ThemePreference, "system">;

export type ThemeState = {
  preference: ThemePreference;
  resolvedTheme: ResolvedTheme;
};

const STORAGE_KEY = "cosir.theme-preference";
const DARK_SCHEME_QUERY = "(prefers-color-scheme: dark)";
const THEME_COLORS: Record<ResolvedTheme, string> = {
  light: "#ffffff",
  dark: "#0a0a0a",
};

let state: ThemeState = { preference: "system", resolvedTheme: "light" };
const listeners = new Set<() => void>();
let systemQuery: MediaQueryList | null = null;

function logStorageFailure(message: string, error: unknown): void {
  void frontendLog("WARNING", "theme_preference_storage_failed", message, { error }).catch(() => undefined);
}

function isThemePreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

function readPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === null) return "system";
    if (isThemePreference(stored)) return stored;
    logStorageFailure("保存的外观偏好无效，已回退到跟随系统", new TypeError("Invalid theme preference"));
  } catch (error) {
    logStorageFailure("读取外观偏好失败，已回退到跟随系统", error);
  }
  return "system";
}

function resolveTheme(preference: ThemePreference): ResolvedTheme {
  if (preference !== "system") return preference;
  return window.matchMedia(DARK_SCHEME_QUERY).matches ? "dark" : "light";
}

function applyTheme(next: ThemeState): void {
  const root = document.documentElement;
  root.classList.toggle("dark", next.resolvedTheme === "dark");
  document
    .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_COLORS[next.resolvedTheme]);
}

function publish(next: ThemeState): void {
  if (state.preference === next.preference && state.resolvedTheme === next.resolvedTheme) return;
  state = next;
  applyTheme(state);
  listeners.forEach((listener) => listener());
}

function updateSystemTheme(): void {
  if (state.preference === "system") publish({ preference: "system", resolvedTheme: resolveTheme("system") });
}

function handleStorage(event: StorageEvent): void {
  if (event.key !== STORAGE_KEY && event.key !== null) return;
  const preference = event.newValue === null ? "system" : isThemePreference(event.newValue) ? event.newValue : "system";
  publish({ preference, resolvedTheme: resolveTheme(preference) });
}

/**
 * 在 React 挂载前读取用户偏好并接管首屏脚本；只有 system 模式监听操作系统变化。
 * 偏好保存在 WebView localStorage，主题类和系统状态栏颜色则由此模块统一应用。
 */
export function installThemePreferenceSync(): () => void {
  state = { preference: readPreference(), resolvedTheme: "light" };
  state = { ...state, resolvedTheme: resolveTheme(state.preference) };
  applyTheme(state);

  systemQuery = window.matchMedia(DARK_SCHEME_QUERY);
  systemQuery.addEventListener("change", updateSystemTheme);
  window.addEventListener("storage", handleStorage);

  return () => {
    systemQuery?.removeEventListener("change", updateSystemTheme);
    systemQuery = null;
    window.removeEventListener("storage", handleStorage);
  };
}

/** 用户选择立即作用于当前会话；存储失败只影响下次启动，不回滚当前界面。 */
export function setThemePreference(preference: ThemePreference): void {
  if (!isThemePreference(preference)) throw new TypeError("Unsupported theme preference");
  try {
    window.localStorage.setItem(STORAGE_KEY, preference);
  } catch (error) {
    logStorageFailure("保存外观偏好失败，当前会话仍应用所选主题", error);
  }
  publish({ preference, resolvedTheme: resolveTheme(preference) });
}

export function getThemeState(): ThemeState {
  return state;
}

export function useThemeState(): ThemeState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getThemeState,
    getThemeState,
  );
}
