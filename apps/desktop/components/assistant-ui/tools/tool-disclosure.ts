import { useEffect, useRef, useState } from "react";

type ToolDisclosureOptions = {
  /** Whether an execution with no result body should be opened automatically. */
  openWhileRunning?: boolean;
};

/**
 * Manage a tool disclosure without treating the backend execution state as UI content.
 * Long-running tools can stay as a static status row until display_data is available;
 * user toggles are kept until the lifecycle status changes again.
 */
export function useToolDisclosure(
  status: string,
  defaultOpen = false,
  options: ToolDisclosureOptions = {},
): [boolean, (open: boolean) => void] {
  const openWhileRunning = options.openWhileRunning ?? true;
  const wasRunning = useRef(openWhileRunning && status === "running");
  const [open, setOpen] = useState(defaultOpen || (openWhileRunning && status === "running"));

  useEffect(() => {
    if (status === "running") {
      wasRunning.current = openWhileRunning;
      if (openWhileRunning) setOpen(true);
      return;
    }
    if (wasRunning.current) {
      wasRunning.current = false;
      setOpen(false);
    }
  }, [openWhileRunning, status]);

  return [open, setOpen];
}
