import { createElement, type ComponentProps } from "react";
import {
  EyeIcon,
  GlobeIcon,
  NetworkIcon,
  SearchIcon,
  TerminalIcon,
  WrenchIcon,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

const TOOL_ICON_MAP: Record<string, LucideIcon> = {
  eye: EyeIcon,
  globe: GlobeIcon,
  network: NetworkIcon,
  search: SearchIcon,
  terminal: TerminalIcon,
  wrench: WrenchIcon,
};

export function ToolIcon({
  name,
  className,
  ...props
}: ComponentProps<"svg"> & { name?: string }) {
  const Icon = (name && TOOL_ICON_MAP[name]) ?? WrenchIcon;
  return createElement(Icon, { className: cn("size-4 shrink-0", className), ...props });
}
