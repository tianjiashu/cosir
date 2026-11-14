import { Fragment } from "react";

import { Kbd } from "@/components/ui/kbd";
import { formatKeys, shortcutLabel } from "@/lib/keyboard/shortcuts";

/**
 * 输入框下方的键位提示。回车发送 / Shift+回车换行的规则在 `components/composer/composer-editor-policy.ts`，
 * 命令面板取自快捷键登记表；按键文字都经 `formatKeys` 生成，换平台或改键位不会漂移。
 * 只放在新会话页的版面里（`NewConversationFrame`）：对话页输入框下方紧贴消息流，没有留白。
 */
export function NewConversationKeyHints() {
  const hints = [
    { keys: formatKeys("enter"), label: "发送" },
    { keys: formatKeys("shift+enter"), label: "换行" },
    { keys: shortcutLabel("commandPalette"), label: "命令面板" },
  ];
  return (
    <p className="text-muted-foreground/80 mt-3 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 font-mono text-xs">
      {hints.map((hint, index) => (
        <Fragment key={hint.label}>
          {index > 0 && <span aria-hidden="true">·</span>}
          <span className="inline-flex items-center gap-1.5"><Kbd>{hint.keys}</Kbd>{hint.label}</span>
        </Fragment>
      ))}
    </p>
  );
}
