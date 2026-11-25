import { createElement, type ComponentProps } from "react";
import {
  BotIcon,
  CircleHelpIcon,
  ClockIcon,
  EyeIcon,
  FileTextIcon,
  GitCompareIcon,
  GitForkIcon,
  GlobeIcon,
  InfoIcon,
  NetworkIcon,
  PuzzleIcon,
  SearchIcon,
  SendIcon,
  SparklesIcon,
  TerminalIcon,
  type LucideIcon,
  UsersIcon,
  WrenchIcon,
  XIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * 图标名（lucide）的取值表：工具目录（`lib/assistant/tools/tool-catalog.ts`）的 `icon`，以及活动行里不属于调用的
 * 状态图标（`ActivityFeedback.icon`：思考、等待、停止）。没登记的名字回落到扳手。
 */
const TOOL_ICON_MAP: Record<string, LucideIcon> = {
  bot: BotIcon,
  "circle-help": CircleHelpIcon,
  clock: ClockIcon,
  eye: EyeIcon,
  "file-text": FileTextIcon,
  "git-compare": GitCompareIcon,
  "git-fork": GitForkIcon,
  globe: GlobeIcon,
  info: InfoIcon,
  network: NetworkIcon,
  puzzle: PuzzleIcon,
  search: SearchIcon,
  send: SendIcon,
  sparkles: SparklesIcon,
  terminal: TerminalIcon,
  users: UsersIcon,
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
