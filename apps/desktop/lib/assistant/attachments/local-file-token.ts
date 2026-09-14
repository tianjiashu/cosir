/** Stable client-only token used to render a local ordinary attachment inline. */
export const LOCAL_FILE_TOKEN = /\[\[cosir-file:([^\]]+)\]\]/g;

export function localFileTokenIds(text: string): string[] {
  return [...text.matchAll(LOCAL_FILE_TOKEN)].map((match) => match[1]);
}
