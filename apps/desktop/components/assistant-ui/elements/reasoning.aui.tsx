"use client";

import { memo, useRef } from "react";
import {
  useScrollLock,
  type ReasoningMessagePartComponent,
} from "@assistant-ui/react";
import { MarkdownText } from "@/components/markdown-text";
import {
  ANIMATION_DURATION,
  ReasoningRoot as ReasoningRootBase,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
  type ReasoningRootProps,
} from "@/components/reasoning";

/** `ReasoningRoot` with the thread viewport scroll locked during disclosure animations. */
function ReasoningRoot(props: Omit<ReasoningRootProps, "onAnimationStart" | "ref">) {
  const collapsibleRef = useRef<HTMLDivElement | null>(null);
  const lockScroll = useScrollLock(collapsibleRef, ANIMATION_DURATION);

  return (
    <ReasoningRootBase
      {...props}
      ref={collapsibleRef}
      onAnimationStart={lockScroll}
    />
  );
}

const ReasoningImpl: ReasoningMessagePartComponent = ({ status }) => (
  <MarkdownText status={status} />
);

const Reasoning = memo(
  ReasoningImpl,
) as unknown as ReasoningMessagePartComponent & {
  Root: typeof ReasoningRoot;
  Trigger: typeof ReasoningTrigger;
  Content: typeof ReasoningContent;
  Text: typeof ReasoningText;
};

Reasoning.displayName = "Reasoning";
Reasoning.Root = ReasoningRoot;
Reasoning.Trigger = ReasoningTrigger;
Reasoning.Content = ReasoningContent;
Reasoning.Text = ReasoningText;

export {
  Reasoning,
  ReasoningRoot,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
};
