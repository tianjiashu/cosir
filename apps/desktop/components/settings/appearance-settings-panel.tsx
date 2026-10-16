import { MonitorIcon, MoonIcon, SunIcon } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { setThemePreference, useThemeState, type ThemePreference } from "@/lib/theme/theme-preference";

const OPTIONS: readonly {
  value: ThemePreference;
  label: string;
  description: string;
  Icon: typeof SunIcon;
}[] = [
  { value: "light", label: "浅色", description: "始终使用浅色外观", Icon: SunIcon },
  { value: "dark", label: "深色", description: "始终使用深色外观", Icon: MoonIcon },
  { value: "system", label: "跟随系统", description: "与操作系统外观保持一致", Icon: MonitorIcon },
];

/** 应用级用户偏好；主题状态由 theme-preference 模块统一保存并应用。 */
export function AppearanceSettingsPanel() {
  const { preference, resolvedTheme } = useThemeState();
  const effectiveLabel = resolvedTheme === "dark" ? "深色" : "浅色";

  return (
    <Card>
      <CardHeader>
        <div className="space-y-1">
          <CardTitle>外观</CardTitle>
          <CardDescription>选择 Cosir 的颜色主题。此偏好保存在本机。</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <fieldset className="grid gap-3 sm:grid-cols-3">
          <legend className="sr-only">颜色主题</legend>
          {OPTIONS.map(({ value, label, description, Icon }) => (
            <label
              key={value}
              className="has-checked:border-primary has-checked:bg-primary/5 focus-within:ring-ring/50 flex cursor-pointer items-start gap-3 rounded-xl border border-border p-4 transition-colors hover:bg-muted/50 focus-within:ring-3"
            >
              <input
                type="radio"
                name="theme-preference"
                value={value}
                checked={preference === value}
                onChange={() => setThemePreference(value)}
                className="sr-only"
              />
              <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <span className="min-w-0 space-y-1">
                <span className="block text-sm font-medium">{label}</span>
                <span className="block text-xs text-muted-foreground">{description}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
          {preference === "system" ? `当前系统外观：${effectiveLabel}` : `当前使用：${effectiveLabel}`}
        </p>
      </CardContent>
    </Card>
  );
}
