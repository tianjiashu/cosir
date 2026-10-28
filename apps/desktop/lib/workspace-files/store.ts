import type { WorkspaceReference } from "@/lib/workspace-references/codec";
import { create } from "zustand";

type FileTab = { path: string; pinned: boolean };
export type ReadingPosition = {
  offset: number;
  before?: number;
  tail?: boolean;
  scrollTop: number;
  encoding?: string;
  previewScrollTop?: number;
};
export type ReadingPreference = {
  wrap?: boolean;
  mode?: "source" | "preview" | "split";
};
export type FileSession = {
  reference: WorkspaceReference | null;
  preferences: Record<string, ReadingPreference>;
  tabs: FileTab[];
  active: string | null;
  expanded: string[];
  positions: Record<string, ReadingPosition>;
  treeWidth: number;
  treeVisible: boolean;
  browsing: boolean;
};
const fresh = (): FileSession => ({
  reference: null,
  preferences: {},
  tabs: [],
  active: null,
  expanded: [""],
  positions: {},
  treeWidth: 230,
  treeVisible: true,
  browsing: true,
});
const emptySession = fresh();
export const useFilesStore = create<{
  sessions: Record<number, FileSession>;
  patch: (id: number, patch: Partial<FileSession>) => void;
  open: (id: number, path: string, pinned?: boolean) => void;
  close: (id: number, path: string) => void;
  forget: (id: number) => void;
  preference: (id: number, path: string, patch: ReadingPreference) => void;
  position: (id: number, path: string, patch: Partial<ReadingPosition>) => void;
}>((set) => ({
  sessions: {},
  patch: (id, patch) =>
    set((state) => ({
      sessions: {
        ...state.sessions,
        [id]: { ...(state.sessions[id] ?? fresh()), ...patch },
      },
    })),
  open: (id, path, pinned = false) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      const existing = session.tabs.find((tab) => tab.path === path);
      const tabs = existing
        ? session.tabs.map((tab) =>
            tab.path === path ? { ...tab, pinned: tab.pinned || pinned } : tab,
          )
        : [...session.tabs.filter((tab) => tab.pinned), { path, pinned }];
      const positions = Object.fromEntries(
        Object.entries(session.positions).filter(([key]) =>
          tabs.some((tab) => tab.path === key),
        ),
      );
      return {
        sessions: {
          ...state.sessions,
          [id]: { ...session, tabs, active: path, positions, browsing: false, reference: null },
        },
      };
    }),
  close: (id, path) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      const index = session.tabs.findIndex((tab) => tab.path === path);
      const tabs = session.tabs.filter((tab) => tab.path !== path);
      const positions = { ...session.positions };
      delete positions[path];
      return {
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            tabs,
            positions,
            active:
              session.active === path
                ? (tabs[Math.min(index, tabs.length - 1)]?.path ?? null)
                : session.active,
          },
        },
      };
    }),
  forget: (id) =>
    set((state) => {
      const sessions = { ...state.sessions };
      delete sessions[id];
      return { sessions };
    }),
  // 阅读选择属于工作区会话偏好，关闭标签仍保留；不写回后端文件事实。
  preference: (id, path, patch) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      return {
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            preferences: {
              ...session.preferences,
              [path]: { ...session.preferences[path], ...patch },
            },
          },
        },
      };
    }),
  position: (id, path, patch) =>
    set((state) => {
      const session = state.sessions[id] ?? fresh();
      return {
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            positions: {
              ...session.positions,
              [path]: {
                ...(session.positions[path] ?? {
                  offset: 0,
                  scrollTop: 0,
                }),
                ...patch,
              },
            },
          },
        },
      };
    }),
}));
export const fileSession = (
  sessions: Record<number, FileSession>,
  id: number,
) => sessions[id] ?? emptySession;

/**
 * 复制给用户的路径文本。工作区内路径统一用 `/`；绝对路径按工作区根的分隔符拼接，Windows 根
 * （含 `\`）下得到原生的 `C:\repo\src` 形式。根目录的相对路径写作 `.`。
 */
export function workspacePathText(rootPath: string, path: string, absolute: boolean): string {
  if (!absolute) return path || ".";
  const separator = rootPath.includes("\\") ? "\\" : "/";
  const root = rootPath.replace(/[\\/]$/, "");
  return path ? `${root}${separator}${path.replaceAll("/", separator)}` : root;
}

export function parentPath(path: string): string {
  return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}
export function parentsOf(path: string): string[] {
  const parts = path.split("/");
  return [
    "",
    ...parts
      .slice(0, -1)
      .map((_, index) => parts.slice(0, index + 1).join("/")),
  ];
}
