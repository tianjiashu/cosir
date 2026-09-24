import { createElement, type ComponentProps } from "react";
import {
  ClockIcon,
  EyeIcon,
  FileSymlinkIcon,
  FileXIcon,
  GitCompareIcon,
  GlobeIcon,
  InfoIcon,
  NetworkIcon,
  SendIcon,
  UsersIcon,
  SearchIcon,
  TerminalIcon,
  WrenchIcon,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

const TOOL_ICON_MAP: Record<string, LucideIcon> = {
  clock: ClockIcon,
  eye: EyeIcon,
  "git-compare": GitCompareIcon,
  "file-x": FileXIcon,
  "file-symlink": FileSymlinkIcon,
  globe: GlobeIcon,
  info: InfoIcon,
  network: NetworkIcon,
  search: SearchIcon,
  send: SendIcon,
  terminal: TerminalIcon,
  wrench: WrenchIcon,
  users: UsersIcon,
};

export function ToolIcon({
  name,
  className,
  ...props
}: ComponentProps<"svg"> & { name?: string }) {
  const Icon = (name && TOOL_ICON_MAP[name]) ?? WrenchIcon;
  return createElement(Icon, { className: cn("size-4 shrink-0", className), ...props });
}
