import { createElement, type ComponentProps } from "react";
import {
  BotIcon,
  ClockIcon,
  EyeIcon,
  FileSymlinkIcon,
  FileXIcon,
  GitCompareIcon,
  GitForkIcon,
  GlobeIcon,
  InfoIcon,
  KeyboardIcon,
  NetworkIcon,
  PuzzleIcon,
  SearchIcon,
  SendIcon,
  TerminalIcon,
  type LucideIcon,
  UsersIcon,
  WorkflowIcon,
  WrenchIcon,
  XIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

/** 工具目录（`lib/assistant/tools/tool-catalog.ts`）里 `icon` 的取值表（lucide 图标名）；没登记的名字回落到扳手。 */
const TOOL_ICON_MAP: Record<string, LucideIcon> = {
  bot: BotIcon,
  clock: ClockIcon,
  eye: EyeIcon,
  "git-compare": GitCompareIcon,
  "git-fork": GitForkIcon,
  "file-x": FileXIcon,
  "file-symlink": FileSymlinkIcon,
  globe: GlobeIcon,
  info: InfoIcon,
  keyboard: KeyboardIcon,
  network: NetworkIcon,
  puzzle: PuzzleIcon,
  search: SearchIcon,
  send: SendIcon,
  terminal: TerminalIcon,
  users: UsersIcon,
  workflow: WorkflowIcon,
  wrench: WrenchIcon,
  x: XIcon,
};

export function ToolIcon({
  name,
  className,
  ...props
}: ComponentProps<"svg"> & { name?: string }) {
  const Icon = (name && TOOL_ICON_MAP[name]) ?? WrenchIcon;
  return createElement(Icon, { className: cn("size-4 shrink-0", className), ...props });
}
