"use client";

import type { ComponentProps, ReactNode } from "react";
import { ChevronDownIcon } from "lucide-react";
import { CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { DISCLOSURE_ROW_CLASS } from "./disclosure-tokens";

export type DisclosureRowTone = "default" | "terminal";

type DisclosureRowLayoutProps = {
  leading?: ReactNode;
  label: ReactNode;
  meta?: ReactNode;
  trailing?: ReactNode;
  tone?: DisclosureRowTone;
  className?: string;
};

function rowToneClassName(tone: DisclosureRowTone): string {
  return tone === "terminal"
    ? "text-zinc-100 hover:bg-white/5"
    : "text-muted-foreground hover:text-foreground";
}

function DisclosureRowContents({
  leading,
  label,
  meta,
  trailing,
  tone,
  withChevron = false,
}: DisclosureRowLayoutProps & { withChevron?: boolean }) {
  return (
    <>
      <span className="flex size-4 shrink-0 items-center justify-center">
        {leading}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {meta && <span className="shrink-0 text-xs">{meta}</span>}
      {trailing && (
        <span className="flex shrink-0 items-center gap-2">{trailing}</span>
      )}
      {withChevron && (
        <ChevronDownIcon
          data-slot="disclosure-row-chevron"
          className={cn(
            "size-4 shrink-0 -rotate-90 transition-transform duration-(--animation-duration) ease-[cubic-bezier(0.32,0.72,0,1)]",
            "group-data-panel-open/disclosure-row-trigger:rotate-0",
            "motion-reduce:transition-none",
            tone === "terminal" ? "text-zinc-400" : "text-muted-foreground",
          )}
          aria-hidden="true"
        />
      )}
    </>
  );
}

function DisclosureRowLayout({
  leading,
  label,
  meta,
  trailing,
  tone = "default",
  className,
  ...props
}: DisclosureRowLayoutProps & ComponentProps<"div">) {
  return (
    <div
      className={cn(
        DISCLOSURE_ROW_CLASS,
        rowToneClassName(tone),
        className,
      )}
      {...props}
    >
      <DisclosureRowContents
        leading={leading}
        label={label}
        meta={meta}
        trailing={trailing}
        tone={tone}
      />
    </div>
  );
}

export type DisclosureRowProps = Omit<
  ComponentProps<typeof CollapsibleTrigger>,
  "children"
> &
  DisclosureRowLayoutProps;

/**
 * Render a shared disclosure trigger for reasoning and tool trace rows.
 * The chevron follows Base UI's `data-panel-open` state on the trigger;
 * it deliberately does not depend on Radix's `data-state` attribute.
 */
export function DisclosureRow({
  leading,
  label,
  meta,
  trailing,
  tone = "default",
  className,
  ...props
}: DisclosureRowProps) {
  return (
    <CollapsibleTrigger
      data-slot="disclosure-row-trigger"
      className={cn(
        DISCLOSURE_ROW_CLASS,
        "group/disclosure-row-trigger",
        rowToneClassName(tone),
        className,
      )}
      {...props}
    >
      <DisclosureRowContents
        leading={leading}
        label={label}
        meta={meta}
        trailing={trailing}
        tone={tone}
        withChevron
      />
    </CollapsibleTrigger>
  );
}

/** Render the same row geometry for non-expandable tool results. */
export function DisclosureRowStatic({
  ...props
}: DisclosureRowLayoutProps & ComponentProps<"div">) {
  return <DisclosureRowLayout data-slot="disclosure-row" {...props} />;
}
