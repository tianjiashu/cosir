import { createContext, useContext } from "react";

export const AttachmentTaskContext = createContext<number | undefined>(undefined);

export function useAttachmentTaskId(): number | undefined {
  return useContext(AttachmentTaskContext);
}
