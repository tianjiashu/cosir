/**
 * 插件/能力入口组件（占位）。
 *
 * 第一版仅展示静态按钮，点击无实际功能。
 *
 * @module components/sidebar/PluginList
 */

import { Puzzle } from "lucide-react";

/**
 * PluginList 插件/能力入口占位组件。
 *
 * 展示插件入口按钮，后续接入实际插件系统。
 */
export function PluginList() {
  return (
    <div className="px-2 py-1">
      <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground hover:bg-accent/50 hover:text-accent-foreground transition-colors">
        <Puzzle className="h-4 w-4" />
        插件
      </button>
    </div>
  );
}
