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
import { BotIcon, CheckCircle2Icon, CircleHelpIcon, Loader2Icon, Maximize2Icon, NetworkIcon, Redo2Icon, SaveIcon, ShrinkIcon, Undo2Icon, XIcon } from "lucide-react";

import { AgentTeamGraphInspector, type GraphInspectorSelection } from "@/components/agent-team-graph-inspector";
import { TeamGraphIssuesIndicator } from "@/components/agent-team-graph-issues";
import { AgentTeamGraphNode, AgentTeamGraphTerminalNode, AgentTeamGraphUnresolvedEndpoint, GraphNodeViewContext } from "@/components/agent-team-graph-nodes";
import { computeTeamAutoLayout } from "@/components/agent-team-graph-layout";
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
import { FullscreenPortal } from "@/components/ui/fullscreen-portal";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { AgentConfiguration } from "@/lib/api/configuration";
import type { AgentTeamGraphIssue } from "@/lib/api/agent-teams";
import type { TeamGraphIssues } from "@/lib/agent-team-editor/use-graph-issues";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { useThemeState } from "@/lib/theme/theme-preference";
import { newTraceId } from "@/lib/trace";
import { cn } from "@/lib/utils";

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

/** 属性面板（w-72 + 右侧间距）在画布上占用的宽度。 */
const INSPECTOR_RESERVED_WIDTH = 300;

/** Agent 节点的估计尺寸（w-64，高度按两个状态出口估算），用于落点夹取与可见性判断。 */
const AGENT_NODE_SIZE = { width: 256, height: 160 } as const;

const CANVAS_SHORTCUTS = [
  ["拖入 Agent / END", "从左侧工具栏"],
  ["连线", "从状态出口拖到目标"],
  ["改路径", "选中连线后拖控制点"],
  ["适配画布", "F"],
  ["缩放", "+ / -"],
  ["平移", "按住 Space 拖动"],
  ["删除", "Delete"],
  ["撤销 / 重做", "⌘Z / ⇧⌘Z"],
] as const;

function isEditableTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement
    && (target.isContentEditable || target.matches("input, textarea, select, button, [role=\"textbox\"]"));
}

function GraphCanvas({
  session,
  profiles,
  onSave,
  onExit,
  saving,
  scopeEditable,
  teamIdEditable,
  graphIssues,
}: {
  session: TeamEditorSession;
  profiles: AgentConfiguration[];
  onSave: () => void;
  onExit: () => void;
  saving: boolean;
  scopeEditable: boolean;
  teamIdEditable: boolean;
  graphIssues: TeamGraphIssues;
}) {
  const { resolvedTheme } = useThemeState();
  const flow = useReactFlow<TeamGraphNode>();
  const canvasRef = useRef<HTMLDivElement>(null);
  const { snapshot: { configuration, layout }, history, viewport, mode, baseline } = useStore(session.store);
  const commitEdit = session.commit;
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedTransition, setSelectedTransition] = useState<string | null>(null);
  const [edgeRoutePreview, setEdgeRoutePreview] = useState<{ routeKey: string; route: AgentTeamEdgeRoute } | null>(null);
  const [fullscreen, setFullscreen] = useState(false);
  // 新建 Team 先填 ID 与名称，故默认打开 Team 属性；已有 Team 进入画布时保持画布不被遮挡。
  const [teamPanelOpen, setTeamPanelOpen] = useState(() => baseline === null);
  const [arranging, setArranging] = useState(false);
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
      .map((edge) => {
        // 有结构问题的转移改用警告色虚线，与状态色区分开。
        const problem = graphIssues.byTransitionIndex.has(edge.data!.transitionIndex);
        const style = problem ? { ...edge.style, stroke: "var(--warning)", strokeDasharray: "5 4" } : edge.style;
        return edge.id === selectedTransition ? { ...edge, selected: true, style: { ...style, strokeWidth: 2.5 } } : { ...edge, style };
      }),
    [configuration, graphIssues.byTransitionIndex, layout.endTargets, layout.endNodeIds, selectedTransition, visibleEdgeRoutes],
  );

  useEffect(() => {
    if (graphNodesRef.current === graphNodes) return;
    graphNodesRef.current = graphNodes;
    setNodes((current) => reconcileGraphNodes(current, graphNodes));
  }, [graphNodes]);

  // 每次切到画布都先全屏：嵌入在设置页或工作台时画布扣除左栏与属性面板后放不下节点。
  // 画布切走后仍保持挂载（见 AgentTeamConfigurationEditor），故按 mode 变化而非挂载触发；
  // 用户退出全屏后留在嵌入画布，直到下次切回画布。
  useEffect(() => {
    if (mode === "canvas") setFullscreen(true);
  }, [mode]);

  useEffect(() => {
    if (!fullscreen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const exitOnEscape = (event: KeyboardEvent) => {
      // 属性面板文本框里的 Escape 属于输入本身，不退出全屏；按钮上的 Escape 照常退出。
      const typing = event.target instanceof HTMLElement && (event.target.isContentEditable || event.target.matches("input, textarea, select"));
      if (event.key === "Escape" && !event.defaultPrevented && !typing) setFullscreen(false);
    };
    window.addEventListener("keydown", exitOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", exitOnEscape);
    };
  }, [fullscreen]);

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

  const closePanels = () => {
    clearSelection();
    setTeamPanelOpen(false);
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

  /**
   * 一键整理：按当前节点（含实际尺寸）与连线计算分层布局，位置与清空的手动连线路径
   * 作为一次编辑提交（一步撤销）。节点位置变了，旧的手动路径已不再合适，故一并清除。
   * 计算期间用户若又改了图，提交时以最新快照为底，只覆盖参与计算的节点位置。
   */
  const arrangeLayout = async () => {
    const traceId = newTraceId();
    const startedAt = Date.now();
    setArranging(true);
    try {
      const positions = await computeTeamAutoLayout(nodes, edges);
      const current = session.store.getState().snapshot;
      commitEdit({
        configuration: current.configuration,
        layout: { ...current.layout, positions: { ...current.layout.positions, ...positions }, edgeRoutes: {} },
      });
      void frontendLog("INFO", "agent_team_graph_auto_layout_applied", "已整理 Agent Team 画布布局", {
        traceId,
        data: { teamId: current.configuration.team_id, nodeCount: Object.keys(positions).length, durationMs: Date.now() - startedAt },
      });
      // 等新位置渲染后再适配视口。
      window.requestAnimationFrame(() => { void flow.fitView({ padding: 0.22, maxZoom: 1, duration: 240 }); });
    } catch (error) {
      void frontendLog("ERROR", "agent_team_graph_auto_layout_failed", "整理 Agent Team 画布布局失败", {
        traceId,
        data: { teamId: configuration.team_id, nodeCount: nodes.length },
        error,
      });
      notify.error("整理布局失败，画布保持原样", { error, fallback: "排版引擎未能完成计算" });
    } finally {
      setArranging(false);
    }
  };

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
  const deleteTransition = (transitionId: string) => {
    session.remove({ nodeIds: new Set(), endNodeIds: new Set(), transitionIds: new Set([transitionId]) });
    clearSelection();
  };

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
    revealNode(nodePosition);
  };

  /**
   * 保证刚放下的 Agent 节点落在面板左侧的可见区域内，否则保持缩放平移画布把它移进来。
   *
   * 落点经避让搜索可能被推到画布外或属性面板下（新节点随即被选中，面板必然打开）；
   * 在放下之后统一处理，覆盖 + 添加、拖入与避让推开等所有来源，避让算法保持纯坐标计算。
   */
  const revealNode = (position: XYPosition) => {
    const bounds = canvasRef.current?.getBoundingClientRect();
    if (!bounds) return;
    const { x, y, zoom } = flow.getViewport();
    const margin = 16;
    const nodeWidth = AGENT_NODE_SIZE.width * zoom;
    const nodeHeight = AGENT_NODE_SIZE.height * zoom;
    const visibleRight = bounds.width - INSPECTOR_RESERVED_WIDTH;
    const left = x + position.x * zoom;
    const top = y + position.y * zoom;
    const fullyVisible = left >= margin && top >= margin
      && left + nodeWidth <= visibleRight - margin
      && top + nodeHeight <= bounds.height - margin;
    if (fullyVisible) return;
    // 可见区放得下就居中；嵌入式窄画布放不下时贴左，尽量少被面板遮住。
    const targetLeft = visibleRight - 2 * margin >= nodeWidth ? (visibleRight - nodeWidth) / 2 : margin;
    const targetTop = Math.max(margin, (bounds.height - nodeHeight) / 2);
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    void flow.setViewport(
      { x: targetLeft - position.x * zoom, y: targetTop - position.y * zoom, zoom },
      { duration: reduceMotion ? 0 : 200 },
    );
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

  const getAgentNodePositionAt = (clientX: number, clientY: number) => getNodePositionAt(clientX, clientY, AGENT_NODE_SIZE.width, AGENT_NODE_SIZE.height);
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
    session.editTransition(oldTransition ? oldIndex : null, nextTransition, targetIsEnd ? connection.target : undefined);
    // 连线后不自动选中：属性面板浮在画布右侧，连续连线时会挡住下一个目标；需要时点击连线再编辑。
    setSelectedNodeId(null);
    setSelectedTransition(null);
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

  const agentRoles = useMemo(() => new Map(profiles.map((profile) => [profile.agent_id, profile.role])), [profiles]);
  const nodeView = useMemo(() => ({
    selectedNodeId,
    agentRole: (agentId: string) => agentRoles.get(agentId),
    nodeIssues: (nodeId: string) => graphIssues.byNodeId.get(nodeId) ?? [],
  }), [agentRoles, graphIssues.byNodeId, selectedNodeId]);

  // 从问题列表跳到对应位置：转移优先选中连线，否则选中所在节点。
  const selectIssue = (issue: AgentTeamGraphIssue) => {
    const transition = issue.transition_index === null ? undefined : configuration.transitions[issue.transition_index];
    const node = configuration.nodes.find((item) => item.node_id === issue.node_id);
    setTeamPanelOpen(false);
    if (transition) {
      setSelectedNodeId(null);
      setSelectedTransition(transition.editorId);
    } else if (node) {
      setSelectedTransition(null);
      setSelectedNodeId(node.editorId);
      revealNode(nodes.find((item) => item.id === node.editorId)?.position ?? { x: 0, y: 0 });
    }
  };

  let selection: GraphInspectorSelection = null;
  const selectedTransitionIndex = selectedTransition ? configuration.transitions.findIndex((edge) => edge.editorId === selectedTransition) : -1;
  const selectedNodeIndex = selectedNodeId ? configuration.nodes.findIndex((node) => node.editorId === selectedNodeId) : -1;
  if (selectedTransitionIndex >= 0) selection = { kind: "transition", transitionIndex: selectedTransitionIndex, hasCustomRoute: Boolean(layout.edgeRoutes[selectedTransition!]) };
  else if (selectedNodeIndex >= 0) selection = { kind: "agent", nodeIndex: selectedNodeIndex };
  else if (selectedNodeId && layout.endNodeIds.includes(selectedNodeId)) selection = { kind: "terminal", endNodeId: selectedNodeId };

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
      {/* 全屏时顶栏占据窗口最上方一行：让出标题栏安全区，并作为窗口拖动区。 */}
      <header
        data-tauri-drag-region={fullscreen ? "deep" : undefined}
        className={cn(
          "bg-card flex h-14 shrink-0 items-center justify-between gap-3 border-b",
          fullscreen ? "pr-[max(1rem,calc(var(--titlebar-inset-right)+0.5rem))] pl-[max(1rem,calc(var(--titlebar-inset-left)+0.5rem))]" : "px-4",
        )}
      >
        <button type="button" className="hover:bg-muted/60 focus-ring flex min-w-0 items-center gap-3 rounded-lg px-1.5 py-1 text-left transition-colors" aria-label="Team 属性" onClick={() => { clearSelection(); setTeamPanelOpen(true); }}>
          <span className="bg-primary/10 text-primary flex size-8 shrink-0 items-center justify-center rounded-lg"><BotIcon className="size-4" /></span>
          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold">{configuration.name || "新建 Agent Team"}</span>
            <span className="text-muted-foreground block truncate text-2xs">{configuration.nodes.length} 个节点 · {configuration.transitions.length} 条转移</span>
          </span>
        </button>
        <div className="flex shrink-0 items-center gap-1.5">
          <span className="mr-1.5"><TeamGraphIssuesIndicator graphIssues={graphIssues} onSelect={selectIssue} /></span>
          <Button type="button" variant="ghost" size="sm" onClick={() => void arrangeLayout()} disabled={arranging || !configuration.nodes.length} title="按执行顺序自动排列节点（可撤销）">
            {arranging ? <Loader2Icon className="animate-spin" /> : <NetworkIcon className="-rotate-90" />}整理布局
          </Button>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="撤销" title="撤销 (Ctrl+Z / ⌘Z)" onClick={undo} disabled={!history.past.length}><Undo2Icon /></Button>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="重做" title="重做 (Ctrl+Y / Ctrl+Shift+Z / ⌘Shift+Z)" onClick={redo} disabled={!history.future.length}><Redo2Icon /></Button>
          <Button type="button" variant="ghost" size="icon-sm" aria-label={fullscreen ? "退出全屏画布" : "全屏画布"} title={fullscreen ? "退出全屏画布 (Esc)" : "全屏画布"} onClick={() => setFullscreen((value) => !value)}>{fullscreen ? <ShrinkIcon /> : <Maximize2Icon />}</Button>
          <Button type="button" variant="ghost" size="icon-sm" aria-label="返回 Team 编辑" onClick={() => { setFullscreen(false); onExit(); }}><XIcon /></Button>
          <Button type="button" size="sm" className="ml-1.5" onClick={onSave} disabled={saving}><SaveIcon />{saving ? "保存中…" : "保存"}</Button>
        </div>
      </header>
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
          <GraphNodeViewContext.Provider value={nodeView}>
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
              onPaneClick={() => { closePanels(); canvasRef.current?.focus({ preventScroll: true }); }}
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
              <Panel position="bottom-right" className="!bottom-3 !right-3">
                <Popover>
                  <PopoverTrigger render={<Button type="button" variant="outline" size="icon-sm" className="bg-card/90 backdrop-blur" aria-label="画布操作说明" />}>
                    <CircleHelpIcon />
                  </PopoverTrigger>
                  <PopoverContent side="top" align="end" className="w-64">
                    <p className="text-xs font-semibold">画布操作</p>
                    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
                      {CANVAS_SHORTCUTS.map(([action, how]) => <div key={action} className="contents"><dt className="text-muted-foreground">{action}</dt><dd className="text-right font-medium">{how}</dd></div>)}
                    </dl>
                  </PopoverContent>
                </Popover>
              </Panel>
              </ReactFlow>
            </AgentTeamEdgeRouteContext.Provider>
          </GraphNodeViewContext.Provider>
          {/* 表单视图下画布隐藏但保持挂载，面板随之不渲染，避免与表单的同名字段并存。 */}
          {mode === "canvas" && (selection || teamPanelOpen) && <AgentTeamGraphInspector
            selection={selection}
            graphIssues={graphIssues}
            configuration={configuration}
            profiles={profiles}
            endNodeCount={layout.endNodeIds.length}
            teamIdEditable={teamIdEditable}
            scopeEditable={scopeEditable}
            actions={{
              onDismiss: closePanels,
              onUpdateNode: updateNode,
              onSetStartNode: setStartNode,
              onDeleteNode: deleteNode,
              onDeleteEndNode: deleteEndNode,
              onUpdateTransition: (index, changes) => session.editTransition(index, { ...configuration.transitions[index], ...changes }),
              onDeleteTransition: deleteTransition,
              onResetRoute: resetEdgeRoute,
              onUpdateTeam: session.updateConfiguration,
            }}
          />}
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
  onSave: () => void;
  onExit: () => void;
  saving: boolean;
  scopeEditable: boolean;
  teamIdEditable: boolean;
  graphIssues: TeamGraphIssues;
}) {
  return <ReactFlowProvider><GraphCanvas {...props} /></ReactFlowProvider>;
}
