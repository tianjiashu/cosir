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

/** 单个 reasoning part 的正文；折叠外壳由消息分组（group-reasoning）统一提供。 */
const ReasoningImpl: ReasoningMessagePartComponent = ({ status }) => (
  <MarkdownText status={status} />
);

const Reasoning = memo(ReasoningImpl);

Reasoning.displayName = "Reasoning";

export {
  Reasoning,
  ReasoningRoot,
  ReasoningTrigger,
  ReasoningContent,
  ReasoningText,
};
