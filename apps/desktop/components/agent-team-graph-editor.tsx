"use client";

import { useStore } from "zustand";
import type { TeamEditorSession } from "@/lib/agent-team-editor/session";
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
import { BotIcon, CheckCircle2Icon, Maximize2Icon, Redo2Icon, SaveIcon, Settings2Icon, ShrinkIcon, Undo2Icon, XIcon } from "lucide-react";

import { AgentTeamConfigurationFields } from "@/components/agent-team-configuration-fields";
import { AgentTeamGraphNode, AgentTeamGraphTerminalNode, AgentTeamGraphUnresolvedEndpoint, GraphNodeEditorContext } from "@/components/agent-team-graph-nodes";
import { AgentTeamGraphPalette } from "@/components/agent-team-graph-palette";
import { AgentTeamEdgeRouteContext, AgentTeamRoutedEdge } from "@/components/agent-team-routed-edge";
import {
  createGraphEdges,
  createGraphNodes,
  END_NODE_ID,
  findAvailableNodePosition,
  isEndViewId,
  reconcileGraphNodes,
  statusFromHandleId,
  unresolvedEndpointViewId,
  type TeamGraphNode,
  type AgentTeamEdgeRoute,
} from "@/components/agent-team-graph-model";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/state-panel";
import { FullscreenPortal } from "@/components/ui/fullscreen-portal";
import type { AgentConfiguration } from "@/lib/api/configuration";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useThemeState } from "@/lib/theme/theme-preference";
import { newTraceId } from "@/lib/trace";

const nodeTypes = { team: AgentTeamGraphNode, terminal: AgentTeamGraphTerminalNode, unresolved: AgentTeamGraphUnresolvedEndpoint };
const edgeTypes = { teamRouted: AgentTeamRoutedEdge };

/* xyflow 内置控件和连线使用显式生效主题，避免绕过用户覆盖系统外观的选择。 */

type PaletteDrag = {
  pointerId: number;
  startX: number;
  startY: number;
  active: boolean;
  traceId: string;
  startedAt: number;
} & ({ kind: "agent"; agentId: string } | { kind: "end" });

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

function GraphCanvas({
  session,
  profiles,
  onSave,
  onExit,
  error,
  saving,
  scopeEditable,
  teamIdEditable,
}: {
  session: TeamEditorSession;
  profiles: AgentConfiguration[];
  onSave?: () => void;
  onExit?: () => void;
  error?: string | null;
  saving?: boolean;
  scopeEditable?: boolean;
  teamIdEditable: boolean;
}) {
  const { resolvedTheme } = useThemeState();
  const flow = useReactFlow<TeamGraphNode>();
  const canvasRef = useRef<HTMLDivElement>(null);
  const { snapshot: { configuration, layout }, history, viewport } = useStore(session.store);
  const commitEdit = session.commit;
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedTransition, setSelectedTransition] = useState<string | null>(null);
  const [edgeRoutePreview, setEdgeRoutePreview] = useState<{ routeKey: string; route: AgentTeamEdgeRoute } | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [dragGhost, setDragGhost] = useState<({ kind: "agent"; agentId: string } | { kind: "end" }) & { x: number; y: number } | null>(null);
  const pointerDragRef = useRef<PaletteDrag | null>(null);
  const connectionTraceRef = useRef<{ traceId: string; kind: "connect" | "reconnect" } | null>(null);
  const nodeDragTraceRef = useRef(new Map<string, { traceId: string; startedAt: number }>());
  const viewportTraceRef = useRef<string | null>(null);
  const graphNodes = useMemo(
    () => createGraphNodes(configuration, layout.positions, layout.endNodeIds),
    [configuration, layout.positions, layout.endNodeIds],
  );
  const [nodes, setNodes] = useState(graphNodes);
  const graphNodesRef = useRef(graphNodes);
  const visibleEdgeRoutes = useMemo(() => edgeRoutePreview
    ? { ...layout.edgeRoutes, [edgeRoutePreview.routeKey]: edgeRoutePreview.route }
    : layout.edgeRoutes, [edgeRoutePreview, layout.edgeRoutes]);
  const edges = useMemo(
    () => createGraphEdges(configuration, layout.endTargets, layout.endNodeIds, visibleEdgeRoutes)
      .map((edge) => edge.id === selectedTransition ? { ...edge, selected: true } : edge),
    [configuration, layout.endTargets, layout.endNodeIds, selectedTransition, visibleEdgeRoutes],
  );

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
      if (event.key === "Escape" && !event.defaultPrevented && !settingsOpen) setFullscreen(false);
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
    if (selectedTransition && !configuration.transitions.some(edge => edge.editorId === selectedTransition)) setSelectedTransition(null);
  }, [configuration.transitions, graphNodes, selectedNodeId, selectedTransition]);

  const clearSelection = () => {
    setSelectedNodeId(null);
    setSelectedTransition(null);
    setNodes((current) => current.map((node) => node.selected ? { ...node, selected: false } : node));
  };

  const previewEdgeRoute = useCallback((routeKey: string, route: AgentTeamEdgeRoute) => {
    setEdgeRoutePreview({ routeKey, route });
  }, []);

  const commitEdgeRoute = useCallback((routeKey: string, route: AgentTeamEdgeRoute, coalesceKey?: string) => {
    setEdgeRoutePreview(null);
    const current = session.store.getState().snapshot;
    const edgeRoutes = { ...current.layout.edgeRoutes, [routeKey]: route };
    commitEdit({
      configuration: current.configuration,
      layout: { ...current.layout, edgeRoutes },
    }, coalesceKey);
    void frontendLog("INFO", "agent_team_graph_edge_route_changed", "已调整 Agent Team 连线路径", {
      traceId: newTraceId(),
      data: { teamId: current.configuration.team_id, routeKey, axis: route.axis, offset: route.offset },
    });
  }, [commitEdit]);

  const cancelEdgeRoutePreview = useCallback(() => {
    setEdgeRoutePreview(null);
  }, []);

  const resetEdgeRoute = useCallback((routeKey: string) => {
    const current = session.store.getState().snapshot;
    if (!current.layout.edgeRoutes[routeKey]) return;
    const edgeRoutes = { ...current.layout.edgeRoutes };
    delete edgeRoutes[routeKey];
    commitEdit({
      configuration: current.configuration,
      layout: { ...current.layout, edgeRoutes },
    });
    void frontendLog("INFO", "agent_team_graph_edge_route_reset", "已恢复 Agent Team 连线自动路径", {
      traceId: newTraceId(),
      data: { teamId: current.configuration.team_id, routeKey },
    });
  }, [commitEdit]);

  const edgeRouteActions = useMemo(() => ({
    preview: previewEdgeRoute,
    commit: commitEdgeRoute,
    cancel: cancelEdgeRoutePreview,
    reset: resetEdgeRoute,
  }), [cancelEdgeRoutePreview, commitEdgeRoute, previewEdgeRoute, resetEdgeRoute]);

  const undo = () => { session.undo(); clearSelection(); };
  const redo = () => { session.redo(); clearSelection(); };
  const updateNode = session.updateNode;
  const deleteNode = (index: number) => {
    session.remove({ nodeIds: new Set([configuration.nodes[index].editorId]), endNodeIds: new Set(), transitionIds: new Set() });
    clearSelection();
  };
  const setStartNode = (nodeId: string) => session.updateConfiguration({ start_node_id: nodeId });

  const addEndNode = (dropPosition?: XYPosition) => {
    const current = session.store.getState().snapshot;
    const endNodeId = session.allocateEnd();
    const center = canvasRef.current?.getBoundingClientRect();
    const preferredPosition = dropPosition ?? (center
      ? flow.screenToFlowPosition({ x: center.left + center.width * 0.72, y: center.top + center.height * 0.55 })
      : { x: 520, y: 220 + current.layout.endNodeIds.length * 100 });
    const position = findAvailableNodePosition(preferredPosition, nodes.map((node) => node.position));
    commitEdit({
      configuration: current.configuration,
      layout: { ...current.layout, endNodeIds: [...current.layout.endNodeIds, endNodeId], positions: { ...current.layout.positions, [endNodeId]: position } },
    });
    setSelectedNodeId(endNodeId);
    setSelectedTransition(null);
  };

  const deleteEndNode = (endNodeId: string) => {
    session.remove({ nodeIds: new Set(), endNodeIds: new Set([endNodeId]), transitionIds: new Set() });
    clearSelection();
  };

  const addAgentNode = (profile: AgentConfiguration, position?: XYPosition) => {
    const preferredPosition = position ?? { x: 100 + configuration.nodes.length * 300, y: 100 };
    const nodePosition = findAvailableNodePosition(preferredPosition, nodes.map(item => item.position));
    setSelectedNodeId(session.addNode(profile.agent_id, profile.agent_id, nodePosition));
    setSelectedTransition(null);
  };

  const getNodePositionAt = (clientX: number, clientY: number, width: number, height: number): XYPosition | undefined => {
    const bounds = canvasRef.current?.getBoundingClientRect();
    if (!bounds) return undefined;

    // 按节点实际尺寸夹住屏幕坐标，再转换到画布坐标，避免贴边拖入后节点被裁切。
    const zoom = flow.getZoom();
    const nodeWidth = width * zoom;
    const nodeHeight = height * zoom;
    const margin = 20;
    const left = Math.min(Math.max(clientX - nodeWidth / 2, bounds.left + margin), bounds.right - nodeWidth - margin);
    const top = Math.min(Math.max(clientY - nodeHeight / 2, bounds.top + margin), bounds.bottom - nodeHeight - margin);
    return flow.screenToFlowPosition({ x: left, y: top });
  };

  const getAgentNodePositionAt = (clientX: number, clientY: number) => getNodePositionAt(clientX, clientY, 256, 160);
  const getEndNodePositionAt = (clientX: number, clientY: number) => getNodePositionAt(clientX, clientY, 144, 56);

  const commitConnection = (connection: Connection, oldEdge?: Edge): void => {
    const current = session.store.getState().snapshot;
    const sourceIndex = current.configuration.nodes.findIndex(node => node.editorId === connection.source);
    const sourceNode = current.configuration.nodes[sourceIndex];
    const oldIndex = current.configuration.transitions.findIndex(edge => edge.editorId === oldEdge?.id);
    const oldTransition = oldIndex >= 0 ? current.configuration.transitions[oldIndex] : undefined;
    const sourceIsUnresolved = connection.source === (oldTransition && unresolvedEndpointViewId("source", oldTransition.editorId));
    if (!sourceNode && (!sourceIsUnresolved || !oldTransition)) return;
    const source = sourceNode?.node_id ?? oldTransition!.from_node_id;
    const status = sourceNode ? sourceNode.statuses.find(status => status.editorId === connection.sourceHandle)?.name ?? statusFromHandleId(connection.sourceHandle) ?? "" : oldTransition!.status;
    const targetIsEnd = isEndViewId(connection.target);
    const targetIndex = targetIsEnd ? -1 : current.configuration.nodes.findIndex(node => node.editorId === connection.target);
    const targetIsUnresolved = Boolean(oldTransition && connection.target === unresolvedEndpointViewId("target", oldTransition.editorId));
    if (!targetIsEnd && targetIndex < 0 && !targetIsUnresolved) return;
    const targetNodeId = targetIsEnd ? END_NODE_ID : targetIndex >= 0 ? current.configuration.nodes[targetIndex].node_id : oldTransition!.target_node_id;
    // 多个视觉 END 都序列化为同一业务目标；具体落点只保存在本地画布布局中。
    const nextTransition = { from_node_id: source, status, target_node_id: targetNodeId };
    const id = session.editTransition(oldTransition ? oldIndex : null, nextTransition, targetIsEnd ? connection.target : undefined);
    setSelectedNodeId(null);
    setSelectedTransition(id);
  };

  const logConnectionOutcome = (kind: "connect" | "reconnect", connection: Connection, oldEdge?: Edge) => {
    void frontendLog("INFO", `agent_team_graph_${kind}_committed`, "画布连线已写入编辑草稿", {
      traceId: connectionTraceRef.current?.traceId,
      data: {
        teamId: configuration.team_id,
        source: connection.source,
        sourceHandle: connection.sourceHandle,
        target: connection.target,
        targetHandle: connection.targetHandle,
        oldEdgeId: oldEdge?.id,
        transitionCount: session.store.getState().snapshot.configuration.transitions.length,
      },
    });
  };

  const onConnect: OnConnect = (connection) => {
    commitConnection(connection);
    logConnectionOutcome("connect", connection);
  };

  const onReconnect: OnReconnect = (oldEdge, connection) => {
    commitConnection(connection, oldEdge);
    logConnectionOutcome("reconnect", connection, oldEdge);
  };

  const onNodesChange = useCallback((changes: NodeChange<TeamGraphNode>[]) => {
    const settledPositions = Object.fromEntries(changes.flatMap((change) => {
      if (change.type !== "position" || change.dragging === true || !change.position || nodeDragTraceRef.current.has(change.id)) return [];
      return [[change.id, change.position] as const];
    }));
    if (Object.keys(settledPositions).length) {
      const current = session.store.getState().snapshot;
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
    const current = session.store.getState().snapshot;
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
    const selectedNodes = flow.getNodes().filter(node => node.selected);
    const transitionIds = new Set(flow.getEdges().filter(edge => edge.selected).map(edge => edge.id));
    if (selectedTransition) transitionIds.add(selectedTransition);
    session.remove({
      nodeIds: new Set(selectedNodes.filter(node => node.data.kind === "agent").map(node => node.id)),
      endNodeIds: new Set(selectedNodes.filter(node => node.data.kind === "terminal").map(node => node.id)),
      transitionIds,
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
    session.setViewport(viewport);
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
      kind: "agent",
      agentId: profile.agent_id,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      active: false,
      traceId: newTraceId(),
      startedAt: Date.now(),
    };
  };

  const beginEndDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    pointerDragRef.current = {
      kind: "end",
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
      void frontendLog("INFO", drag.kind === "agent" ? "agent_team_graph_palette_drag_started" : "agent_team_graph_end_palette_drag_started", drag.kind === "agent" ? "用户开始拖拽子 Agent 到画布" : "用户开始拖拽 END 到画布", {
        traceId: drag.traceId,
        data: { teamId: configuration.team_id, ...(drag.kind === "agent" ? { agentId: drag.agentId } : {}), startX: drag.startX, startY: drag.startY },
      });
    }
    setDragGhost(drag.kind === "agent"
      ? { kind: "agent", agentId: drag.agentId, x: event.clientX, y: event.clientY }
      : { kind: "end", x: event.clientX, y: event.clientY });
    event.preventDefault();
  };

  const finishAgentDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = pointerDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dropZone = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-agent-drop-zone]");
    if (drag.active && dropZone) {
      if (drag.kind === "agent") {
        const position = getAgentNodePositionAt(event.clientX, event.clientY);
        const profile = profiles.find((item) => item.agent_id === drag.agentId);
        if (profile) {
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
      } else {
        const position = getEndNodePositionAt(event.clientX, event.clientY);
        addEndNode(position);
        void frontendLog("INFO", "agent_team_graph_end_palette_drop_completed", "END 已拖入画布", {
          traceId: drag.traceId,
          data: { teamId: configuration.team_id, position, durationMs: Date.now() - drag.startedAt },
        });
      }
    } else if (drag.active) {
      void frontendLog("INFO", drag.kind === "agent" ? "agent_team_graph_palette_drag_cancelled" : "agent_team_graph_end_palette_drag_cancelled", drag.kind === "agent" ? "子 Agent 拖拽未落在画布区域" : "END 拖拽未落在画布区域", {
        traceId: drag.traceId,
        data: { teamId: configuration.team_id, ...(drag.kind === "agent" ? { agentId: drag.agentId } : {}), dropZoneFound: false },
      });
    }
    pointerDragRef.current = null;
    setDragGhost(null);
  };

  const cancelAgentDrag = () => {
    const drag = pointerDragRef.current;
    if (drag?.active) {
      void frontendLog("INFO", drag.kind === "agent" ? "agent_team_graph_palette_drag_cancelled" : "agent_team_graph_end_palette_drag_cancelled", drag.kind === "agent" ? "子 Agent 拖拽被系统取消" : "END 拖拽被系统取消", {
        traceId: drag.traceId,
        data: { teamId: configuration.team_id, ...(drag.kind === "agent" ? { agentId: drag.agentId } : {}), reason: "pointer_cancel" },
      });
    }
    pointerDragRef.current = null;
    setDragGhost(null);
  };

  const editor = (
    <section
      onPointerMove={moveAgentDrag}
      onPointerUp={finishAgentDrag}
      onPointerCancel={cancelAgentDrag}
      className={fullscreen
        ? "bg-background fixed inset-0 z-40 flex h-dvh w-screen flex-col overflow-hidden"
        : "bg-background flex h-[min(76vh,820px)] min-h-[600px] w-full flex-col overflow-hidden"}
      aria-label="Agent Team 画布编辑器"
    >
      <header className="bg-card flex shrink-0 items-center justify-between gap-3 border-b px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <div className="bg-primary/10 text-primary flex size-9 shrink-0 items-center justify-center rounded-xl"><BotIcon className="size-4" /></div>
          <div className="min-w-0"><h2 className="truncate text-sm font-semibold">{configuration.name || "新建 Agent Team"}</h2></div>
          <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
            <Button type="button" variant="outline" size="sm" onClick={() => setSettingsOpen(true)}><Settings2Icon />Team 设置</Button>
            <DialogContent>
              <DialogHeader><DialogTitle>Team 设置</DialogTitle><DialogDescription>编辑 Team 信息和执行上限。</DialogDescription></DialogHeader>
              <AgentTeamConfigurationFields
                configuration={configuration}
                teamIdEditable={teamIdEditable}
                scopeEditable={scopeEditable ?? false}
                layout="dialog"
                onChange={session.updateConfiguration}
              />
            </DialogContent>
          </Dialog>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button type="button" variant="outline" size="icon-sm" aria-label="撤销" title="撤销 (Ctrl+Z / ⌘Z)" onClick={undo} disabled={!history.past.length}><Undo2Icon /></Button>
          <Button type="button" variant="outline" size="icon-sm" aria-label="重做" title="重做 (Ctrl+Y / Ctrl+Shift+Z / ⌘Shift+Z)" onClick={redo} disabled={!history.future.length}><Redo2Icon /></Button>
          <Button type="button" variant="outline" size="icon-sm" aria-label={fullscreen ? "退出全屏画布" : "全屏画布"} title={fullscreen ? "退出全屏画布 (Esc)" : "全屏画布"} onClick={() => setFullscreen((value) => !value)}>{fullscreen ? <ShrinkIcon /> : <Maximize2Icon />}</Button>
          {onExit && <Button type="button" variant="ghost" size="icon-sm" aria-label="返回 Team 编辑" onClick={() => { setFullscreen(false); onExit(); }}><XIcon /></Button>}
          {onSave && <Button type="button" size="sm" onClick={onSave} disabled={saving}><SaveIcon />{saving ? "保存中…" : "保存"}</Button>}
        </div>
      </header>
      {error && <div role="alert" className="shrink-0 border-b border-destructive/20 bg-destructive/10 px-4 py-2 text-xs text-destructive">{error}</div>}
      <div className="flex min-h-0 flex-1">
        <AgentTeamGraphPalette profiles={profiles} onAdd={addProfileToCenter} onBeginDrag={beginAgentDrag} onBeginEndDrag={beginEndDrag} />
        <div
          ref={canvasRef}
          data-agent-drop-zone
          className="relative min-w-0 flex-1 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40"
          tabIndex={0}
          aria-label="Agent Team 画布。使用 Tab 浏览节点和连线，方向键移动选中节点或微调选中连线。"
          onKeyDown={handleCanvasKeyDown}
        >
          <GraphNodeEditorContext.Provider value={{
            selectedNodeId,
            endNodeCount: layout.endNodeIds.length,
            getCanvasBounds: () => canvasRef.current?.getBoundingClientRect() ?? null,
            onDismissNode: () => setSelectedNodeId(null),
            onUpdateNode: updateNode,
            onSetStartNode: setStartNode,
            onDeleteNode: deleteNode,
            onDeleteEndNode: deleteEndNode,
          }}>
            <AgentTeamEdgeRouteContext.Provider value={edgeRouteActions}>
              <ReactFlow
              colorMode={resolvedTheme}
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              onNodesChange={onNodesChange}
              onConnect={onConnect}
              onConnectStart={(_, params) => {
                if (connectionTraceRef.current?.kind === "reconnect") return;
                const traceId = newTraceId();
                connectionTraceRef.current = { traceId, kind: "connect" };
                void frontendLog("INFO", "agent_team_graph_connection_started", "开始拖动画布连线", {
                  traceId,
                  data: { teamId: configuration.team_id, nodeId: params.nodeId, handleId: params.handleId, handleType: params.handleType },
                });
              }}
              onConnectEnd={(_, connectionState) => {
                const trace = connectionTraceRef.current;
                if (trace?.kind !== "connect") return;
                void frontendLog(connectionState.isValid ? "INFO" : "WARNING", "agent_team_graph_connection_ended", "结束画布连线拖动", {
                  traceId: trace?.traceId,
                  data: {
                    teamId: configuration.team_id,
                    isValid: connectionState.isValid,
                    sourceNodeId: connectionState.fromNode?.id,
                    sourceHandleId: connectionState.fromHandle?.id,
                    targetNodeId: connectionState.toNode?.id,
                    targetHandleId: connectionState.toHandle?.id,
                    gestureOutcome: connectionState.isValid ? "connected" : "no_compatible_handle",
                  },
                });
                connectionTraceRef.current = null;
              }}
              onReconnect={onReconnect}
              onReconnectStart={(_, edge, handleType) => {
                const traceId = newTraceId();
                connectionTraceRef.current = { traceId, kind: "reconnect" };
                void frontendLog("INFO", "agent_team_graph_reconnection_started", "开始拖动画布连线端点", {
                  traceId,
                  data: { teamId: configuration.team_id, edgeId: edge.id, source: edge.source, target: edge.target, handleType },
                });
              }}
              onReconnectEnd={(_, edge, handleType, connectionState) => {
                const trace = connectionTraceRef.current;
                void frontendLog(connectionState.isValid ? "INFO" : "WARNING", "agent_team_graph_reconnection_ended", "结束画布连线端点拖动", {
                  traceId: trace?.traceId,
                  data: {
                    teamId: configuration.team_id,
                    edgeId: edge.id,
                    handleType,
                    isValid: connectionState.isValid,
                    sourceNodeId: connectionState.fromNode?.id,
                    sourceHandleId: connectionState.fromHandle?.id,
                    targetNodeId: connectionState.toNode?.id,
                    targetHandleId: connectionState.toHandle?.id,
                  },
                });
                connectionTraceRef.current = null;
              }}
              onNodeClick={(_, node) => { setSelectedNodeId(node.id); setSelectedTransition(null); canvasRef.current?.focus({ preventScroll: true }); }}
              onEdgeClick={(_, edge) => { setSelectedNodeId(null); setSelectedTransition(edge.id); canvasRef.current?.focus({ preventScroll: true }); }}
              onPaneClick={() => { setSelectedNodeId(null); setSelectedTransition(null); canvasRef.current?.focus({ preventScroll: true }); }}
              edgesReconnectable
              reconnectRadius={24}
              deleteKeyCode={null}
              defaultViewport={viewport}
              fitView={!viewport}
              fitViewOptions={{ padding: 0.22, maxZoom: 1 }}
              minZoom={0.2}
              maxZoom={1.8}
              panActivationKeyCode="Space"
              ariaLabelConfig={{
                "node.a11yDescription.default": "按 Enter 或空格选择节点，使用方向键移动节点。",
                "edge.a11yDescription.default": "按 Enter 或空格选择转移；拖动线上控制点调整路径，拖动连线端点可更换目标，按 Delete 可删除转移。",
              }}
              onNodeDragStart={onNodeDragStart}
              onNodeDragStop={onNodeDragStop}
              onMoveStart={onViewportMoveStart}
              onMoveEnd={onViewportMoveEnd}
            >
              <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="var(--border)" />
              <Controls position="bottom-left" showInteractive={false} />
              <Panel position="bottom-right" className="pointer-events-none !bottom-4 !right-4">
                <div className="bg-card/90 text-muted-foreground flex items-center gap-3 rounded-xl border px-3 py-2 text-2xs shadow-sm backdrop-blur">
                  <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-primary" />START</span>
                  <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-muted-foreground" />STEP</span>
                  <span className="flex items-center gap-1"><span className="size-2 rounded-full bg-success" />END</span>
                </div>
              </Panel>
              <Panel position="top-right" className="pointer-events-none !right-4 !top-4">
                <div className="bg-card/90 text-muted-foreground rounded-full border px-3 py-1.5 text-2xs shadow-sm backdrop-blur">拖 Agent · 选中连线后拖控制点改路径 · F 适配 · +/- 缩放 · Space 平移 · Delete 删除 · Ctrl/⌘Z 撤销</div>
              </Panel>
              </ReactFlow>
            </AgentTeamEdgeRouteContext.Provider>
          </GraphNodeEditorContext.Provider>
          {!configuration.nodes.length && <div className="pointer-events-none absolute inset-0 flex items-center justify-center"><EmptyState className="bg-background/90 py-6 shadow-sm backdrop-blur" icon={<BotIcon className="text-primary size-7" />} title="从左侧添加第一个 Agent" description="拖拽 Agent 或 END 到画布，或点击 Agent 卡片上的 +" /></div>}
        </div>
      </div>
      {dragGhost && <div aria-hidden="true" className={`${dragGhost.kind === "end" ? "bg-success text-success-foreground" : "bg-primary text-primary-foreground"} pointer-events-none fixed z-[200] rounded-lg px-3 py-2 text-xs font-medium shadow-xl`} style={{ left: dragGhost.x + 14, top: dragGhost.y + 14 }}>{dragGhost.kind === "end" ? <CheckCircle2Icon className="mr-2 inline size-3.5" /> : <BotIcon className="mr-2 inline size-3.5" />}{dragGhost.kind === "end" ? "END" : dragGhost.agentId}</div>}
    </section>
  );
  return <FullscreenPortal active={fullscreen}>{editor}</FullscreenPortal>;
}

export function AgentTeamGraphEditor(props: {
  session: TeamEditorSession;
  profiles: AgentConfiguration[];
  onSave?: () => void;
  onExit?: () => void;
  error?: string | null;
  saving?: boolean;
  scopeEditable?: boolean;
  teamIdEditable: boolean;
}) {
  return <ReactFlowProvider><GraphCanvas {...props} /></ReactFlowProvider>;
}
