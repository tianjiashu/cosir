"use client";

import type { ComponentProps } from "react";

type ComposerInputProps = ComponentProps<"textarea"> & {
  onTextChange?: (text: string) => void;
  onSubmitText?: () => void;
};

export function ComposerInput({
  onTextChange,
  onSubmitText,
  onChange,
  onKeyDown,
  ...props
}: ComposerInputProps) {
  return (
    <textarea
      {...props}
      onChange={(event) => {
        onChange?.(event);
        onTextChange?.(event.currentTarget.value);
      }}
      onKeyDown={(event) => {
        onKeyDown?.(event);
        if (event.defaultPrevented || event.nativeEvent.isComposing) return;
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          onSubmitText?.();
        }
      }}
    />
  );
}
