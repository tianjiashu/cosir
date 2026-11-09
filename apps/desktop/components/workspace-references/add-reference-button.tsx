import { MessageSquarePlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useReferenceBridge } from "./reference-bridge";
import type { Capsule } from "@/lib/assistant/capsules";
export function AddReferenceButton({
  workspaceId,
  capsules,
  label,
}: {
  workspaceId: number;
  capsules: Capsule[];
  label: string;
}) {
  const bridge = useReferenceBridge();
  return (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      disabled={!bridge?.canAdd(workspaceId) || !capsules.length}
      onClick={() => void bridge?.add(workspaceId, capsules)}
    >
      <MessageSquarePlusIcon className="size-3.5" />
      {label}
    </Button>
  );
}
