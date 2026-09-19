import { createElement, type ComponentProps } from "react";
import {
  EyeIcon,
  FileSymlinkIcon,
  FileXIcon,
  GitCompareIcon,
  GlobeIcon,
  NetworkIcon,
  UsersIcon,
  SearchIcon,
  TerminalIcon,
  WrenchIcon,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

const TOOL_ICON_MAP: Record<string, LucideIcon> = {
  eye: EyeIcon,
  "git-compare": GitCompareIcon,
  "file-x": FileXIcon,
  "file-symlink": FileSymlinkIcon,
  globe: GlobeIcon,
  network: NetworkIcon,
  search: SearchIcon,
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
