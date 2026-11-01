import { PanelLeftIcon, SquarePenIcon } from "lucide-react";

import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { withShortcut } from "@/lib/keyboard/shortcuts";

/**
 * 侧栏开关：钉在窗口左上角红绿灯右侧，侧栏展开、收起、窄屏抽屉打开时都在同一个位置、同一个图标，
 * 状态只由 `aria-expanded` 表达——连续点两次不用追着按钮走（Claude 桌面版、ChatGPT、Arc 的做法）。
 * 侧栏不可见时旁边多一个「新对话」，侧栏里的同名入口此时看不到。
 *
 * 不做跨列浮层：AppLayout 把它放进此刻左上角那一行（侧栏可见时是侧栏头部，否则是页面头部的 `leading`），
 * 两行的左内边距都是 `max(1rem, --titlebar-inset-left)`，所以位置不变。按钮因此是所在拖动区的后代，由
 * globals.css 的 app-drag-region 规则排除出拖动；做成浮层会被 DOM 里排在后面的拖动区盖掉，Electron 里
 * 点击就变成拖窗口。
 */
export function SidebarControls({ open, onToggle, onNewConversation }: {
  open: boolean;
  onToggle: () => void;
  onNewConversation: () => void;
}) {
  return (
    <div className="flex shrink-0 items-center gap-1">
      <TooltipIconButton
        tooltip={withShortcut(open ? "收起侧栏" : "展开侧栏", "toggleSidebar")}
        aria-label="切换侧栏"
        aria-expanded={open}
        className="size-7"
        onClick={onToggle}
      >
        <PanelLeftIcon />
      </TooltipIconButton>
      {!open && (
        <TooltipIconButton tooltip={withShortcut("新对话", "newConversation")} aria-label="新对话" className="size-7" onClick={onNewConversation}>
          <SquarePenIcon />
        </TooltipIconButton>
      )}
    </div>
  );
}
