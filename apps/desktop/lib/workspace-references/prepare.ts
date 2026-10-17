import type { WorkspaceReference } from "./codec";

export type ReferenceInput =
  | { path: string; kind: "file" }
  | { path: string; kind: "directory" }
  | { path: string; kind: "snippet"; content: string }
  | {
      path: string;
      kind: "snippet";
      content: string;
      start_line: number;
      end_line: number;
    }
  | { path: string; kind: "excerpt"; content: string };

const MAX_FRAGMENT_BYTES = 256 * 1024;

export function prepareReference(input: ReferenceInput): WorkspaceReference {
  if (input.kind === "file" || input.kind === "directory")
    return { kind: input.kind, path: input.path };
  if (
    !input.content ||
    new TextEncoder().encode(input.content).length > MAX_FRAGMENT_BYTES
  )
    throw new Error("选区为空或超过 256 KiB，请缩小范围");
  const content = input.content.replace(/\r\n?/g, "\n").replace(/\n$/, "");
  return {
    kind: input.kind,
    path: input.path,
    content,
    ...(input.kind === "snippet" && "start_line" in input
      ? { startLine: input.start_line, endLine: input.end_line }
      : {}),
  };
}
