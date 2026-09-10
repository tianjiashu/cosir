import { useEffect, useRef, useState } from "react";

/**
 * Keep a tool panel open while its backend invocation is running, then close it
 * once after the invocation reaches a terminal state. User toggles are kept
 * until the lifecycle status changes again.
 */
export function useToolDisclosure(status: string, defaultOpen = false): [boolean, (open: boolean) => void] {
  const wasRunning = useRef(status === "running");
  const [open, setOpen] = useState(defaultOpen || status === "running");

  useEffect(() => {
    if (status === "running") {
      wasRunning.current = true;
      setOpen(true);
      return;
    }
    if (wasRunning.current) {
      wasRunning.current = false;
      setOpen(false);
    }
  }, [status]);

  return [open, setOpen];
}
