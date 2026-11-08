import { createContext, useContext } from "react";

export const AttachmentWorkspaceContext = createContext<number | undefined>(undefined);

export function useAttachmentWorkspaceId(): number | undefined {
  return useContext(AttachmentWorkspaceContext);
}
