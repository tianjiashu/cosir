import { MessageSquarePlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useReferenceBridge } from "./reference-bridge";
import type { ReferenceInput } from "@/lib/workspace-references/prepare";
export function AddReferenceButton({
  workspaceId,
  inputs,
  label,
}: {
  workspaceId: number;
  inputs: ReferenceInput[];
  label: string;
}) {
  const bridge = useReferenceBridge();
  return (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      disabled={
        !bridge?.target ||
        bridge.workspaceId !== workspaceId ||
        !inputs.length
      }
      onClick={() => void bridge?.add(workspaceId, inputs)}
    >
      <MessageSquarePlusIcon className="size-3.5" />
      {label}
    </Button>
  );
}
