"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type Edge,
  type NodeChange,
  type OnConnect,
  type OnMoveEnd,
  type OnMoveStart,
  type OnNodeDrag,
  type OnReconnect,
  type XYPosition,
} from "@xyflow/react";
import { BotIcon, Maximize2Icon, PlusIcon, Redo2Icon, SaveIcon, Settings2Icon, ShrinkIcon, Undo2Icon, XIcon } from "lucide-react";

import { AgentTeamGraphInspector } from "@/components/agent-team-graph-inspector";
import { AgentTeamConfigurationFields } from "@/components/agent-team-configuration-fields";
import { AgentTeamGraphNode, AgentTeamGraphTerminalNode, GraphNodeEditorContext } from "@/components/agent-team-graph-nodes";
import { AgentTeamGraphPalette } from "@/components/agent-team-graph-palette";
import {
  commitGraphEdit,
  createGraphEditHistory,
  redoGraphEdit,
  undoGraphEdit,
  type AgentTeamGraphSnapshot,
} from "@/components/agent-team-graph-history";
import {
  createGraphEdges,
  createGraphNodes,
  DEFAULT_END_VIEW_ID,
  END_VIEW_ID_PREFIX,
  END_NODE_ID,
  findAvailableNodePosition,
  isEndViewId,
  removeNodesFromConfiguration,
  removeGraphSelection,
  reconcileGraphNodes,
  statusFromHandleId,
  transitionKey,
  updateNodeInConfiguration,
  upsertTransition,
  type TeamGraphNode,
  type AgentTeamGraphLayout,
} from "@/components/agent-team-graph-model";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamConfiguration, AgentTeamNodeConfiguration } from "@/lib/api/agent-teams";
import { frontendLog } from "@/lib/logging/frontend-log";
import { newTraceId } from "@/lib/trace";

const nodeTypes = { team: AgentTeamGraphNode, terminal: AgentTeamGraphTerminalNode };

function isPosition(value: unknown): value is XYPosition {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as XYPosition;
  return typeof candidate.x === "number" && Number.isFinite(candidate.x)
    && typeof candidate.y === "number" && Number.isFinite(candidate.y);
}

function readLayout(storageKey: string): AgentTeamGraphLayout {
  if (typeof window === "undefined") return { positions: {}, endNodeIds: [DEFAULT_END_VIEW_ID], endTargets: {} };
  const serialized = window.localStorage.getItem(storageKey);
  if (!serialized) return { positions: {}, endNodeIds: [DEFAULT_END_VIEW_ID], endTargets: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(serialized);
  } catch {
    return { positions: {}, endNodeIds: [DEFAULT_END_VIEW_ID], endTargets: {} };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { positions: {}, endNodeIds: [DEFAULT_END_VIEW_ID], endTargets: {} };
  }

  const stored = parsed as Record<string, unknown>;
  const rawPositions = stored.positions && typeof stored.positions === "object" && !Array.isArray(stored.positions)
    ? stored.positions as Record<string, unknown>
    : stored;
  const positions = Object.fromEntries(Object.entries(rawPositions).filter((entry): entry is [string, XYPosition] => isPosition(entry[1])));
  const endNodeIds = Array.isArray(stored.endNodeIds)
    ? [...new Set(stored.endNodeIds.filter((id): id is string => typeof id === "string" && isEndViewId(id)))]
    : [DEFAULT_END_VIEW_ID];
  const endTargets = typeof stored.endTargets === "object" && stored.endTargets !== null && !Array.isArray(stored.endTargets)
    ? Object.fromEntries(Object.entries(stored.endTargets).filter((entry): entry is [string, string] => typeof entry[1] === "string" && isEndViewId(entry[1]) && endNodeIds.includes(entry[1])))
    : {};

  return { positions, endNodeIds: endNodeIds.length ? endNodeIds : [DEFAULT_END_VIEW_ID], endTargets };
}

function renderedNodeSnapshot(container: HTMLDivElement | null) {
  return Array.from(container?.querySelectorAll<HTMLElement>(".react-flow__node") ?? [], (element) => {
    const bounds = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    return {
      id: element.dataset.id,
      bounds: { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height },
      display: style.display,
      visibility: style.visibility,
      opacity: style.opacity,
    };
  });
}

function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement
    && (target.isContentEditable || target.matches("input, textarea, select, button, [role=\"textbox\"]"));
}

function nextNodeId(nodes: AgentTeamNodeConfiguration[]): string {
  let suffix = nodes.length + 1;
  while (nodes.some((node) => node.node_id === `node_${suffix}`)) suffix += 1;
  return `node_${suffix}`;
}

function nextEndViewId(endNodeIds: string[]): string {
  let suffix = 1;
  while (endNodeIds.includes(`${END_VIEW_ID_PREFIX}${suffix}`)) suffix += 1;
  return `${END_VIEW_ID_PREFIX}${suffix}`;
}

// 节点位置、结束口数量和线条落点只描述用户的本地画布布局，不进入 Team 配置。
function useGraphLayout(storageKey: string) {
  const [layout, setLayout] = useState(() => readLayout(storageKey));
  const layoutRef = useRef(layout);
  const storageKeyRef = useRef(storageKey);
  layoutRef.current = layout;

  useEffect(() => {
    if (storageKeyRef.current === storageKey) return;
    storageKeyRef.current = storageKey;
    const next = readLayout(storageKey);
    layoutRef.current = next;
    setLayout(next);
  }, [storageKey]);

  useEffect(() => {
    const timeout = window.setTimeout(() => window.localStorage.setItem(storageKey, JSON.stringify(layoutRef.current)), 250);
    return () => window.clearTimeout(timeout);
  }, [layout, storageKey]);

  useEffect(() => () => {
    window.localStorage.setItem(storageKey, JSON.stringify(layoutRef.current));
  }, [storageKey]);

  const replaceLayout = useCallback((next: AgentTeamGraphLayout) => {
    layoutRef.current = next;
    setLayout(next);
  }, []);

  return { layout, replaceLayout };
}

function GraphCanvas({
  configuration,
  profiles,
  storageKey,
  onChange,
  onSave,
  onExit,
  error,
  saving,
  scopeEditable,
  teamIdEditable,
}: {
  configuration: AgentTeamConfiguration;
  profiles: AgentConfiguration[];
  storageKey: string;
  onChange: (configuration: AgentTeamConfiguration) => void;
  onSave?: () => void;
  onExit?: () => void;
  error?: string | null;
  saving?: boolean;
  scopeEditable?: boolean;
  teamIdEditable: boolean;
}) {
  const flow = useReactFlow<TeamGraphNode>();
  const canvasRef = useRef<HTMLDivElement>(null);
  const { layout, replaceLayout } = useGraphLayout(storageKey);
  const documentRef = useRef<AgentTeamGraphSnapshot>({ configuration, layout });
  const historyRef = useRef(createGraphEditHistory());
  const [history, setHistory] = useState(historyRef.current);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedTransition, setSelectedTransition] = useState<string | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [dragGhost, setDragGhost] = useState<{ agentId: string; x: number; y: number } | null>(null);
  const pointerDragRef = useRef<{ agentId: string; pointerId: number; startX: number; startY: number; active: boolean; traceId: string; startedAt: number } | null>(null);
  const nodeDragTraceRef = useRef(new Map<string, { traceId: string; startedAt: number }>());
  const viewportTraceRef = useRef<string | null>(null);
  const graphNodes = useMemo(
    () => createGraphNodes(configuration, layout.positions, layout.endNodeIds),
    [configuration, layout.positions, layout.endNodeIds],
  );
  const [nodes, setNodes] = useState(graphNodes);
  const graphNodesRef = useRef(graphNodes);
  const edges = useMemo(
    () => createGraphEdges(configuration, layout.endTargets, layout.endNodeIds)
      .map((edge) => edge.id === selectedTransition ? { ...edge, selected: true } : edge),
    [configuration, layout.endTargets, layout.endNodeIds, selectedTransition],
  );

  useEffect(() => {
    if (configuration !== documentRef.current.configuration) {
      documentRef.current = { configuration, layout };
      historyRef.current = createGraphEditHistory();
      setHistory(historyRef.current);
    }
  }, [configuration, layout]);

  useEffect(() => {
    if (graphNodesRef.current === graphNodes) return;
    graphNodesRef.current = graphNodes;
    setNodes((current) => reconcileGraphNodes(current, graphNodes));
  }, [graphNodes]);

  useEffect(() => {
    if (!fullscreen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const exitOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !settingsOpen) setFullscreen(false);
    };
    window.addEventListener("keydown", exitOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", exitOnEscape);
    };
  }, [fullscreen, settingsOpen]);

  useEffect(() => {
    if (!selectedNodeId && !selectedTransition) return;
    if (selectedNodeId && !graphNodes.some((node) => node.id === selectedNodeId)) setSelectedNodeId(null);
    if (selectedTransition && !configuration.transitions.some((item) => transitionKey(item.from_node_id, item.status) === selectedTransition)) setSelectedTransition(null);
  }, [configuration.transitions, graphNodes, selectedNodeId, selectedTransition]);

  const applySnapshot = (snapshot: AgentTeamGraphSnapshot) => {
    documentRef.current = snapshot;
    onChange(snapshot.configuration);
    replaceLayout(snapshot.layout);
  };

  const clearSelection = () => {
    setSelectedNodeId(null);
    setSelectedTransition(null);
    setNodes((current) => current.map((node) => node.selected ? { ...node, selected: false } : node));
  };

  const logHistoryAction = (event: string, message: string, data: Record<string, unknown>) => {
    void frontendLog("INFO", event, message, { traceId: newTraceId(), data: { teamId: configuration.team_id, ...data } });
  };

  const commitEdit = (snapshot: AgentTeamGraphSnapshot, coalesceKey?: string) => {
    const current = documentRef.current;
    if (snapshot.configuration === current.configuration && snapshot.layout === current.layout) return;
    historyRef.current = commitGraphEdit(historyRef.current, current, snapshot, { coalesceKey });
    setHistory(historyRef.current);
    applySnapshot(snapshot);
  };

  const undo = () => {
    const result = undoGraphEdit(historyRef.current, documentRef.current);
    if (!result) return;
    historyRef.current = result.history;
    setHistory(result.history);
    applySnapshot(result.snapshot);
    clearSelection();
    logHistoryAction("agent_team_graph_undo", "已撤销 Agent Team 画布编辑", { remainingUndoCount: result.history.past.length, remainingRedoCount: result.history.future.length });
  };

  const redo = () => {
    const result = redoGraphEdit(historyRef.current, documentRef.current);
    if (!result) return;
    historyRef.current = result.history;
    setHistory(result.history);
    applySnapshot(result.snapshot);
    clearSelection();
    logHistoryAction("agent_team_graph_redo", "已重做 Agent Team 画布编辑", { remainingUndoCount: result.history.past.length, remainingRedoCount: result.history.future.length });
  };

  const updateConfiguration = (next: AgentTeamConfiguration, coalesceKey?: string) => {
    const current = documentRef.current;
    const previousIds = new Set(current.configuration.nodes.map((node) => node.node_id));
    const nextIds = new Set(next.nodes.map((node) => node.node_id));
    const removed = [...previousIds].filter((id) => !nextIds.has(id));
    const added = [...nextIds].filter((id) => !previousIds.has(id));
    let nextLayout = current.layout;
    if (removed.length === 1 && added.length === 1) {
      const oldId = removed[0];
      const newId = added[0];
      if (selectedNodeId === oldId) setSelectedNodeId(newId);
      const positions = { ...nextLayout.positions };
      if (positions[oldId]) positions[newId] = positions[oldId];
      delete positions[oldId];
      const endTargets = { ...nextLayout.endTargets };
      for (const transition of current.configuration.transitions) {
        if (transition.from_node_id !== oldId || transition.target_node_id !== END_NODE_ID) continue;
        const oldKey = transitionKey(oldId, transition.status);
        const nextKey = transitionKey(newId, transition.status);
        if (endTargets[oldKey]) endTargets[nextKey] = endTargets[oldKey];
        delete endTargets[oldKey];
      }
      nextLayout = { ...nextLayout, positions, endTargets };
    }
    // 结束口落点属于画布偏好；删除或改名业务转移时同步清理对应的视觉映射。
    const validEndTransitions = new Set(next.transitions.filter((item) => item.target_node_id === END_NODE_ID).map((item) => transitionKey(item.from_node_id, item.status)));
    nextLayout = {
      ...nextLayout,
      endTargets: Object.fromEntries(Object.entries(nextLayout.endTargets).filter(([key, endId]) => validEndTransitions.has(key) && nextLayout.endNodeIds.includes(endId))),
    };
    commitEdit({ configuration: next, layout: nextLayout }, coalesceKey);
  };

  const updateNode = (nodeId: string, changes: Partial<AgentTeamNodeConfiguration>) => {
    const current = documentRef.current.configuration;
    const coalesceKey = Object.keys(changes).length === 1 && "name" in changes ? `node-name:${nodeId}` : undefined;
    updateConfiguration(updateNodeInConfiguration(current, nodeId, changes), coalesceKey);
  };

  const deleteNode = (nodeId: string) => {
    const current = documentRef.current;
    const nextConfiguration = removeNodesFromConfiguration(current.configuration, new Set([nodeId]));
    const positions = { ...current.layout.positions };
    delete positions[nodeId];
    const validEndTransitions = new Set(nextConfiguration.transitions.filter((item) => item.target_node_id === END_NODE_ID).map((item) => transitionKey(item.from_node_id, item.status)));
    const endTargets = Object.fromEntries(Object.entries(current.layout.endTargets).filter(([key, endId]) => validEndTransitions.has(key) && current.layout.endNodeIds.includes(endId)));
    commitEdit({ configuration: nextConfiguration, layout: { ...current.layout, positions, endTargets } });
    logHistoryAction("agent_team_graph_node_deleted", "已删除 Agent Team 画布节点", { nodeId, remainingNodeCount: nextConfiguration.nodes.length });
    clearSelection();
  };

  const deleteTransition = (key: string) => {
    const current = documentRef.current;
    const transitions = current.configuration.transitions.filter((item) => transitionKey(item.from_node_id, item.status) !== key);
    const endTargets = { ...current.layout.endTargets };
    delete endTargets[key];
    commitEdit({
      configuration: { ...current.configuration, transitions },
      layout: { ...current.layout, endTargets },
    });
    logHistoryAction("agent_team_graph_transition_deleted", "已删除 Agent Team 状态转移", { transitionKey: key, remainingTransitionCount: transitions.length });
    setSelectedTransition(null);
  };

  const setStartNode = (nodeId: string) => {
    const current = documentRef.current;
    updateConfiguration({ ...current.configuration, start_node_id: nodeId });
  };

  const addEndNode = () => {
    const current = documentRef.current;
    const endNodeId = nextEndViewId(current.layout.endNodeIds);
    const center = canvasRef.current?.getBoundingClientRect();
    const preferredPosition = center
      ? flow.screenToFlowPosition({ x: center.left + center.width * 0.72, y: center.top + center.height * 0.55 })
      : { x: 520, y: 220 + current.layout.endNodeIds.length * 100 };
    const position = findAvailableNodePosition(preferredPosition, nodes.map((node) => node.position));
    commitEdit({
      configuration: current.configuration,
      layout: { ...current.layout, endNodeIds: [...current.layout.endNodeIds, endNodeId], positions: { ...current.layout.positions, [endNodeId]: position } },
    });
    setSelectedNodeId(endNodeId);
    setSelectedTransition(null);
  };

  const deleteEndNode = (endNodeId: string) => {
    const current = documentRef.current;
    if (current.layout.endNodeIds.length <= 1) return;
    const remaining = current.layout.endNodeIds.filter((id) => id !== endNodeId);
    const fallback = remaining[0];
    const positions = { ...current.layout.positions };
    delete positions[endNodeId];
    const endTargets = Object.fromEntries(Object.entries(current.layout.endTargets).map(([key, id]) => [key, id === endNodeId ? fallback : id]));
    commitEdit({ configuration: current.configuration, layout: { ...current.layout, endNodeIds: remaining, positions, endTargets } });
    logHistoryAction("agent_team_graph_end_node_deleted", "已删除 Agent Team 结束节点", { endNodeId, remainingEndNodeCount: remaining.length });
    clearSelection();
  };

  const addAgentNode = (profile: AgentConfiguration, position?: XYPosition) => {
    const current = documentRef.current;
    const nodeId = nextNodeId(current.configuration.nodes);
    const node: AgentTeamNodeConfiguration = {
      node_id: nodeId,
      name: profile.agent_id,
      agent_id: profile.agent_id,
      statuses: ["done"],
    };
    const endNodeId = current.layout.endTargets[transitionKey(nodeId, "done")] ?? current.layout.endNodeIds[0];
    const preferredPosition = position ?? { x: 100 + current.configuration.nodes.length * 300, y: 100 };
    const nodePosition = findAvailableNodePosition(preferredPosition, nodes.map((item) => item.position));
    const nextConfiguration = {
      ...current.configuration,
      nodes: [...current.configuration.nodes, node],
      start_node_id: current.configuration.start_node_id || nodeId,
      // 新状态先接到结束出口，用户可继续拖线改成中间节点，避免默认悬空。
      transitions: upsertTransition(current.configuration.transitions, { from_node_id: nodeId, status: "done", target_node_id: END_NODE_ID }),
    };
    commitEdit({
      configuration: nextConfiguration,
      layout: {
        ...current.layout,
        positions: { ...current.layout.positions, [nodeId]: nodePosition },
        endTargets: { ...current.layout.endTargets, [transitionKey(nodeId, "done")]: endNodeId },
      },
    });
    setSelectedNodeId(nodeId);
    setSelectedTransition(null);
  };

  const getAgentNodePositionAt = (clientX: number, clientY: number): XYPosition | undefined => {
    const bounds = canvasRef.current?.getBoundingClientRect();
    if (!bounds) return undefined;

    // 用屏幕坐标先夹住节点卡片，再转换到画布坐标，避免贴边拖入后节点被画布裁切。
    const zoom = flow.getZoom();
    const nodeWidth = 256 * zoom;
    const nodeHeight = 160 * zoom;
    const margin = 20;
    const left = Math.min(Math.max(clientX - nodeWidth / 2, bounds.left + margin), bounds.right - nodeWidth - margin);
    const top = Math.min(Math.max(clientY - nodeHeight / 2, bounds.top + margin), bounds.bottom - nodeHeight - margin);
    return flow.screenToFlowPosition({ x: left, y: top });
  };

  const commitConnection = (connection: Connection, oldEdge?: Edge) => {
    const current = documentRef.current;
    const source = connection.source;
    const status = statusFromHandleId(connection.sourceHandle);
    const sourceNode = current.configuration.nodes.find((node) => node.node_id === source);
    const targetIsEnd = isEndViewId(connection.target);
    const validTarget = targetIsEnd ? current.layout.endNodeIds.includes(connection.target) : current.configuration.nodes.some((node) => node.node_id === connection.target);
    if (!sourceNode || !validTarget || status === null || !sourceNode.statuses.includes(status)) return;

    const oldKey = oldEdge ? transitionKey(oldEdge.source, statusFromHandleId(oldEdge.sourceHandle) ?? "") : null;
    const nextKey = transitionKey(source, status);
    if (oldKey && oldKey !== nextKey && current.configuration.transitions.some((item) => transitionKey(item.from_node_id, item.status) === nextKey)) return;
    const withoutOld = oldKey
      ? current.configuration.transitions.filter((item) => transitionKey(item.from_node_id, item.status) !== oldKey)
      : current.configuration.transitions;
    const targetNodeId = targetIsEnd ? END_NODE_ID : connection.target;
    // 多个视觉 END 都序列化为同一业务目标；具体落点只保存在本地画布布局中。
    const transitions = upsertTransition(withoutOld, { from_node_id: source, status, target_node_id: targetNodeId });
    const endTargets = { ...current.layout.endTargets };
    if (oldKey && oldKey !== nextKey) delete endTargets[oldKey];
    if (targetIsEnd) endTargets[nextKey] = connection.target!;
    else delete endTargets[nextKey];
    commitEdit({
      configuration: { ...current.configuration, transitions },
      layout: { ...current.layout, endTargets },
    });
    setSelectedNodeId(null);
    setSelectedTransition(nextKey);
  };

  const onConnect: OnConnect = (connection) => commitConnection(connection);

  const onReconnect: OnReconnect = (oldEdge, connection) => commitConnection(connection, oldEdge);

  const onNodesChange = useCallback((changes: NodeChange<TeamGraphNode>[]) => {
    const settledPositions = Object.fromEntries(changes.flatMap((change) => {
      if (change.type !== "position" || change.dragging === true || !change.position || nodeDragTraceRef.current.has(change.id)) return [];
      return [[change.id, change.position] as const];
    }));
    if (Object.keys(settledPositions).length) {
      const current = documentRef.current;
      const positions = { ...current.layout.positions, ...settledPositions };
      const hasChanged = Object.entries(settledPositions).some(([id, position]) => {
        const previous = current.layout.positions[id];
        return !previous || previous.x !== position.x || previous.y !== position.y;
      });
      if (hasChanged) commitEdit({ configuration: current.configuration, layout: { ...current.layout, positions } }, "keyboard-node-move");
    }
    setNodes((current) => applyNodeChanges(changes, current));
  }, [commitEdit]);

  const onNodeDragStart = useCallback<OnNodeDrag<TeamGraphNode>>((_event, node, nodes) => {
    const traceId = newTraceId();
    const startedAt = Date.now();
    for (const draggedNode of nodes) nodeDragTraceRef.current.set(draggedNode.id, { traceId, startedAt });
    const canvasNodes = flow.getNodes();
    void frontendLog("INFO", "agent_team_graph_node_drag_started", "用户开始拖动画布节点", {
      traceId,
      data: {
        teamId: configuration.team_id,
        nodeId: node.id,
        nodeKind: node.data.kind,
        position: node.position,
        draggedNodeCount: nodes.length,
        canvasNodeCount: canvasNodes.length,
        configurationNodeIds: configuration.nodes.map(({ node_id }) => node_id),
        endNodeIds: layout.endNodeIds,
        viewport: flow.getViewport(),
      },
    });
  }, [configuration.nodes, configuration.team_id, flow, layout.endNodeIds]);

  const onNodeDragStop = useCallback<OnNodeDrag<TeamGraphNode>>((_event, node, nodes) => {
    const started = nodeDragTraceRef.current.get(node.id);
    for (const draggedNode of nodes) nodeDragTraceRef.current.delete(draggedNode.id);
    const canvasNodes = flow.getNodes();
    void frontendLog("INFO", "agent_team_graph_node_drag_completed", "画布节点拖动完成", {
      traceId: started?.traceId ?? newTraceId(),
      data: {
        teamId: configuration.team_id,
        nodeId: node.id,
        nodeKind: node.data.kind,
        position: node.position,
        draggedNodeCount: nodes.length,
        canvasNodeCount: canvasNodes.length,
        configurationNodeIds: configuration.nodes.map(({ node_id }) => node_id),
        endNodeIds: layout.endNodeIds,
        canvasNodes: canvasNodes.map(({ id, position }) => ({ id, position })),
        renderedNodes: renderedNodeSnapshot(canvasRef.current),
        viewport: flow.getViewport(),
        durationMs: started ? Date.now() - started.startedAt : null,
      },
    });
    const current = documentRef.current;
    const finalNodes = new Map(canvasNodes.map((item) => [item.id, item]));
    for (const draggedNode of nodes) finalNodes.set(draggedNode.id, draggedNode);
    finalNodes.set(node.id, node);
    const positions = Object.fromEntries([...finalNodes.values()].map(({ id, position }) => [id, position]));
    const hasChanged = Object.entries(positions).some(([id, position]) => {
      const previous = current.layout.positions[id];
      return !previous || previous.x !== position.x || previous.y !== position.y;
    });
    if (hasChanged) commitEdit({ configuration: current.configuration, layout: { ...current.layout, positions } });
  }, [commitEdit, configuration.nodes, configuration.team_id, flow, layout.endNodeIds]);

  const deleteSelection = () => {
    const selectedNodes = flow.getNodes().filter((node) => node.selected);
    const selectedAgentIds = new Set(selectedNodes.flatMap((node) => node.data.kind === "agent" ? [node.id] : []));
    const selectedEndIds = selectedNodes.flatMap((node) => node.data.kind === "terminal" ? [node.id] : []);
    const selectedTransitionKeys = new Set(flow.getEdges().filter((edge) => edge.selected).map((edge) => edge.id));
    if (selectedTransition) selectedTransitionKeys.add(selectedTransition);
    const current = documentRef.current;
    const next = removeGraphSelection(current.configuration, current.layout, {
      nodeIds: selectedAgentIds,
      endNodeIds: selectedEndIds,
      transitionKeys: selectedTransitionKeys,
    });
    if (next.configuration === current.configuration && next.layout === current.layout) return;
    commitEdit(next);
    logHistoryAction("agent_team_graph_selection_deleted", "已删除 Agent Team 画布选区", {
      deletedAgentNodeCount: current.configuration.nodes.length - next.configuration.nodes.length,
      deletedEndNodeCount: current.layout.endNodeIds.length - next.layout.endNodeIds.length,
      deletedTransitionCount: current.configuration.transitions.length - next.configuration.transitions.length,
    });
    clearSelection();
  };

  const handleCanvasKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (isEditableTarget(event.target)) return;

    const key = event.key.toLowerCase();
    if ((event.metaKey || event.ctrlKey) && key === "z") {
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
      return;
    }
    if (event.ctrlKey && key === "y") {
      event.preventDefault();
      redo();
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey) return;

    if (event.key === "Delete" || event.key === "Backspace") {
      const hasSelection = flow.getNodes().some((node) => node.selected) || flow.getEdges().some((edge) => edge.selected) || selectedTransition !== null;
      if (hasSelection) {
        event.preventDefault();
        deleteSelection();
      }
      return;
    }

    if (key === "f") {
      event.preventDefault();
      void flow.fitView({ padding: 0.22, maxZoom: 1, duration: 180 });
    } else if (event.key === "+" || (event.key === "=" && event.shiftKey)) {
      event.preventDefault();
      void flow.zoomIn({ duration: 120 });
    } else if (event.key === "-") {
      event.preventDefault();
      void flow.zoomOut({ duration: 120 });
    } else if (event.key === " ") {
      // 阻止页面滚动；Space 仍由 React Flow 作为画布平移激活键处理。
      event.preventDefault();
    }
  };

  const onViewportMoveStart = useCallback<OnMoveStart>(() => {
    viewportTraceRef.current = newTraceId();
  }, []);

  const onViewportMoveEnd = useCallback<OnMoveEnd>((_event, viewport) => {
    const canvasRect = canvasRef.current?.getBoundingClientRect();
    void frontendLog("INFO", "agent_team_graph_viewport_changed", "画布视口移动或缩放完成", {
      traceId: viewportTraceRef.current ?? newTraceId(),
      data: {
        teamId: configuration.team_id,
        viewport,
        configurationNodeIds: configuration.nodes.map(({ node_id }) => node_id),
        endNodeIds: layout.endNodeIds,
        canvasNodeCount: flow.getNodes().length,
        canvasNodes: flow.getNodes().map(({ id, position }) => ({ id, position })),
        renderedNodes: renderedNodeSnapshot(canvasRef.current),
        canvasSize: canvasRect ? { width: canvasRect.width, height: canvasRect.height } : null,
      },
    });
    viewportTraceRef.current = null;
  }, [configuration.nodes, configuration.team_id, flow, layout.endNodeIds]);

  const addProfileToCenter = (profile: AgentConfiguration) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    const position = rect ? getAgentNodePositionAt(rect.left + rect.width / 2, rect.top + rect.height / 2) : undefined;
    addAgentNode(profile, position);
  };

  const beginAgentDrag = (profile: AgentConfiguration, event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0 || (event.target instanceof Element && event.target.closest("button"))) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    pointerDragRef.current = {
      agentId: profile.agent_id,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      active: false,
      traceId: newTraceId(),
      startedAt: Date.now(),
    };
  };

  const moveAgentDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = pointerDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    if (!drag.active && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 8) return;
    if (!drag.active) {
      drag.active = true;
      void frontendLog("INFO", "agent_team_graph_palette_drag_started", "用户开始拖拽子 Agent 到画布", {
        traceId: drag.traceId,
        data: { teamId: configuration.team_id, agentId: drag.agentId, startX: drag.startX, startY: drag.startY },
      });
    }
    setDragGhost({ agentId: drag.agentId, x: event.clientX, y: event.clientY });
    event.preventDefault();
  };

  const finishAgentDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = pointerDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dropZone = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-agent-drop-zone]");
    if (drag.active && dropZone) {
      const profile = profiles.find((item) => item.agent_id === drag.agentId);
      if (profile) {
        const position = getAgentNodePositionAt(event.clientX, event.clientY);
        addAgentNode(profile, position);
        void frontendLog("INFO", "agent_team_graph_palette_drop_completed", "子 Agent 已拖入画布", {
          traceId: drag.traceId,
          data: { teamId: configuration.team_id, agentId: drag.agentId, position, durationMs: Date.now() - drag.startedAt },
        });
      } else {
        void frontendLog("WARNING", "agent_team_graph_palette_drop_ignored", "拖拽结束时子 Agent 已不可用", {
          traceId: drag.traceId,
          data: { teamId: configuration.team_id, agentId: drag.agentId },
        });
      }
    } else if (drag.active) {
      void frontendLog("INFO", "agent_team_graph_palette_drag_cancelled", "子 Agent 拖拽未落在画布区域", {
        traceId: drag.traceId,
        data: { teamId: configuration.team_id, agentId: drag.agentId, dropZoneFound: false },
      });
    }
    pointerDragRef.current = null;
    setDragGhost(null);
  };

  const cancelAgentDrag = () => {
    const drag = pointerDragRef.current;
    if (drag?.active) {
      void frontendLog("INFO", "agent_team_graph_palette_drag_cancelled", "子 Agent 拖拽被系统取消", {
        traceId: drag.traceId,
        data: { teamId: configuration.team_id, agentId: drag.agentId, reason: "pointer_cancel" },
      });
    }
    pointerDragRef.current = null;
    setDragGhost(null);
  };

  const isValidConnection = (connection: Connection | Edge) => {
    const status = statusFromHandleId(connection.sourceHandle);
    return Boolean(status
      && configuration.nodes.some((node) => node.node_id === connection.source && node.statuses.includes(status))
      && (layout.endNodeIds.includes(connection.target ?? "") || configuration.nodes.some((node) => node.node_id === connection.target)));
  };

  return (
    <section
      onPointerMove={moveAgentDrag}
      onPointerUp={finishAgentDrag}
      onPointerCancel={cancelAgentDrag}
      className={fullscreen
        ? "bg-background fixed inset-0 z-40 flex h-dvh w-screen flex-col overflow-hidden"
        : "border-border/70 bg-background flex h-[min(76vh,820px)] min-h-[600px] flex-col overflow-hidden rounded-2xl border shadow-sm"}
      aria-label="Agent Team 画布编辑器"
    >
      <header className="bg-card flex shrink-0 items-center justify-between gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="bg-primary/10 text-primary flex size-9 shrink-0 items-center justify-center rounded-xl"><BotIcon className="size-4" /></div>
          <div className="min-w-0"><h2 className="truncate text-sm font-semibold">{configuration.name || "新建 Agent Team"}</h2><p className="text-muted-foreground truncate text-[11px]">{configuration.team_id || "未命名 Team"} · 拖拽子 Agent 创建节点</p></div>
          <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
            <Button type="button" variant="outline" size="sm" onClick={() => setSettingsOpen(true)}><Settings2Icon />Team 设置</Button>
            <DialogContent>
              <DialogHeader><DialogTitle>Team 设置</DialogTitle><DialogDescription>编辑 Team 信息和执行上限。</DialogDescription></DialogHeader>
              <AgentTeamConfigurationFields
                configuration={configuration}
                teamIdEditable={teamIdEditable}
                scopeEditable={scopeEditable ?? false}
                layout="dialog"
                onChange={(changes, historyKey) => updateConfiguration({ ...configuration, ...changes }, historyKey)}
              />
            </DialogContent>
          </Dialog>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button type="button" variant="outline" size="icon-sm" aria-label="撤销" title="撤销 (Ctrl+Z / ⌘Z)" onClick={undo} disabled={!history.past.length}><Undo2Icon /></Button>
          <Button type="button" variant="outline" size="icon-sm" aria-label="重做" title="重做 (Ctrl+Y / Ctrl+Shift+Z / ⌘Shift+Z)" onClick={redo} disabled={!history.future.length}><Redo2Icon /></Button>
          <Button type="button" variant="outline" size="sm" onClick={addEndNode}><PlusIcon />添加 END</Button>
          <Button type="button" variant="outline" size="icon-sm" aria-label={fullscreen ? "退出全屏画布" : "全屏画布"} title={fullscreen ? "退出全屏画布 (Esc)" : "全屏画布"} onClick={() => setFullscreen((value) => !value)}>{fullscreen ? <ShrinkIcon /> : <Maximize2Icon />}</Button>
          {onExit && <Button type="button" variant="ghost" size="icon-sm" aria-label="返回 Team 编辑" onClick={() => { setFullscreen(false); onExit(); }}><XIcon /></Button>}
          {onSave && <Button type="button" size="sm" onClick={onSave} disabled={saving}><SaveIcon />{saving ? "保存中…" : "保存"}</Button>}
        </div>
      </header>
      {error && <div role="alert" className="shrink-0 border-b border-destructive/20 bg-destructive/10 px-4 py-2 text-xs text-destructive">{error}</div>}
      <div className="flex min-h-0 flex-1">
        <AgentTeamGraphPalette profiles={profiles} onAdd={addProfileToCenter} onBeginDrag={beginAgentDrag} />
        <div
          ref={canvasRef}
          data-agent-drop-zone
          className="relative min-w-0 flex-1 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40"
          tabIndex={0}
          aria-label="Agent Team 画布。使用 Tab 浏览节点和连线，方向键移动选中节点。"
          onKeyDown={handleCanvasKeyDown}
        >
          <GraphNodeEditorContext.Provider value={{
            selectedNodeId,
            endNodeCount: layout.endNodeIds.length,
            getCanvasBounds: () => canvasRef.current?.getBoundingClientRect() ?? null,
            isNodeIdAvailable: (candidate, current) => !configuration.nodes.some((node) => node.node_id === candidate && node.node_id !== current),
            onDismissNode: () => setSelectedNodeId(null),
            onUpdateNode: updateNode,
            onSetStartNode: setStartNode,
            onDeleteNode: deleteNode,
            onDeleteEndNode: deleteEndNode,
          }}>
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onConnect={onConnect}
              onReconnect={onReconnect}
              isValidConnection={isValidConnection}
              onNodeClick={(_, node) => { setSelectedNodeId(node.id); setSelectedTransition(null); canvasRef.current?.focus({ preventScroll: true }); }}
              onEdgeClick={(_, edge) => { setSelectedNodeId(null); setSelectedTransition(edge.id); canvasRef.current?.focus({ preventScroll: true }); }}
              onPaneClick={() => { setSelectedNodeId(null); setSelectedTransition(null); canvasRef.current?.focus({ preventScroll: true }); }}
              edgesReconnectable
              deleteKeyCode={null}
              fitView
              fitViewOptions={{ padding: 0.22, maxZoom: 1 }}
              minZoom={0.2}
              maxZoom={1.8}
              panActivationKeyCode="Space"
              ariaLabelConfig={{
                "node.a11yDescription.default": "按 Enter 或空格选择节点，使用方向键移动节点。",
                "edge.a11yDescription.default": "按 Enter 或空格选择转移，在画布浮层中编辑目标或删除转移。",
              }}
              onNodeDragStart={onNodeDragStart}
              onNodeDragStop={onNodeDragStop}
              onMoveStart={onViewportMoveStart}
              onMoveEnd={onViewportMoveEnd}
            >
              <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--border)" />
              <Controls position="bottom-left" showInteractive={false} />
              <Panel position="bottom-right" className="pointer-events-none !bottom-4 !right-4">
                <div className="bg-card/90 text-muted-foreground flex items-center gap-3 rounded-xl border px-3 py-2 text-[10px] shadow-sm backdrop-blur">
                  <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-primary" />START</span>
                  <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-muted-foreground" />STEP</span>
                  <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-emerald-600" />END</span>
                </div>
              </Panel>
              <Panel position="top-right" className="pointer-events-none !right-4 !top-4">
                <div className="bg-card/90 text-muted-foreground rounded-full border px-3 py-1.5 text-[10px] shadow-sm backdrop-blur">拖 Agent / 连线 · F 适配 · +/- 缩放 · Space 平移 · Delete 删除 · Ctrl/⌘Z 撤销</div>
              </Panel>
            </ReactFlow>
          </GraphNodeEditorContext.Provider>
          <AgentTeamGraphInspector
            configuration={configuration}
            selectedTransitionKey={selectedTransition}
            onChange={updateConfiguration}
            onDismiss={() => setSelectedTransition(null)}
            onDeleteTransition={deleteTransition}
          />
          {!configuration.nodes.length && <div className="pointer-events-none absolute inset-0 flex items-center justify-center"><div className="bg-background/90 rounded-2xl border border-dashed px-7 py-6 text-center shadow-sm backdrop-blur"><BotIcon className="text-primary mx-auto size-7" /><h3 className="mt-3 text-sm font-semibold">从左侧添加第一个 Agent</h3><p className="text-muted-foreground mt-1 text-xs">拖拽到画布，或点击 Agent 卡片上的 +</p></div></div>}
        </div>
      </div>
      {dragGhost && <div aria-hidden="true" className="bg-primary text-primary-foreground pointer-events-none fixed z-[200] rounded-lg px-3 py-2 text-xs font-medium shadow-xl" style={{ left: dragGhost.x + 14, top: dragGhost.y + 14 }}><BotIcon className="mr-2 inline size-3.5" />{dragGhost.agentId}</div>}
    </section>
  );
}

export function AgentTeamGraphEditor(props: {
  configuration: AgentTeamConfiguration;
  profiles: AgentConfiguration[];
  storageKey: string;
  onChange: (configuration: AgentTeamConfiguration) => void;
  onSave?: () => void;
  onExit?: () => void;
  error?: string | null;
  saving?: boolean;
  scopeEditable?: boolean;
  teamIdEditable: boolean;
}) {
  return <ReactFlowProvider key={props.storageKey}><GraphCanvas {...props} /></ReactFlowProvider>;
}
