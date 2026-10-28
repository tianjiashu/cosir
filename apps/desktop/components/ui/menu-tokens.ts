/**
 * 下拉菜单与右键菜单（radix DropdownMenu / ContextMenu）的基础外观：弹层底板与菜单项。
 *
 * 各处只在此基础上用 `cn` 追加自身差异（宽度、滚动、图标间距、选中态等），不再各写一份完整类名。
 */
export const MENU_CONTENT_CLASS = "z-50 rounded-md border bg-popover p-1 text-sm shadow-md";

export const MENU_ITEM_CLASS = "cursor-default rounded px-2 py-1.5 outline-none focus:bg-muted";
