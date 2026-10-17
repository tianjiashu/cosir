import { useMemo, useRef, useState } from "react";
import type { ContextMessage } from "@/lib/api/context-messages";
import {
  filterMessages,
  indexMessages,
  INITIAL_FILTERS,
  type MessageFilters,
} from "./message-model";

export type ListScroll = { sequence: number } | { offset: number };

/** 查询范围变化由页面 key 重建；同范围刷新只替换数据，保留筛选、定位与滚动意图。 */
export function useMessageNavigation(
  messages: ContextMessage[],
  initialSequence?: number,
) {
  const [filters, setFilters] = useState<MessageFilters>(INITIAL_FILTERS);
  const [chosen, setChosen] = useState<number | null>(initialSequence ?? null);
  const [reveal, setReveal] = useState(false);
  const [scroll, setScroll] = useState<ListScroll | null>(
    initialSequence === undefined ? null : { sequence: initialSequence },
  );
  const [detailOpen, setDetailOpen] = useState(false);
  const listOffset = useRef(0);
  const saved = useRef<{ sequence: number | null; offset: number } | null>(
    null,
  );
  const index = useMemo(() => indexMessages(messages), [messages]);
  const filtered = useMemo(
    () => filterMessages(index, filters),
    [index, filters],
  );
  const ordered = useMemo(
    () => [...messages].sort((a, b) => a.sequence - b.sequence),
    [messages],
  );
  const rows = reveal ? ordered : filtered;
  const selected =
    rows.find((message) => message.sequence === chosen) ?? rows[0] ?? null;

  const updateFilters = (patch: Partial<MessageFilters>) => {
    setFilters((current) => ({ ...current, ...patch }));
    setReveal(false);
    saved.current = null;
    setScroll({ offset: 0 });
    setDetailOpen(false);
  };
  const locate = (sequence: number) => {
    if (!reveal)
      saved.current = {
        sequence: selected?.sequence ?? null,
        offset: listOffset.current,
      };
    setReveal(true);
    setChosen(sequence);
    setScroll({ sequence });
    setDetailOpen(true);
  };
  const returnToResults = () => {
    setReveal(false);
    setChosen(saved.current?.sequence ?? null);
    setScroll({ offset: saved.current?.offset ?? 0 });
    saved.current = null;
    setDetailOpen(false);
  };
  return {
    filters,
    updateFilters,
    rows,
    selected,
    reveal,
    locate,
    returnToResults,
    listOffset,
    scroll,
    clearScroll: () => setScroll(null),
    detailOpen,
    setDetailOpen,
    select: (sequence: number, move = false) => {
      setChosen(sequence);
      if (move) setScroll({ sequence });
    },
  };
}
export type MessageNavigation = ReturnType<typeof useMessageNavigation>;
