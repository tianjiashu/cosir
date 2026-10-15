"use client";

import { createContext, useContext, useRef, type PointerEvent as ReactPointerEvent, type KeyboardEvent as ReactKeyboardEvent } from "react";
import {
  BaseEdge,
  EdgeLabelRenderer,
  getSmoothStepPath,
  useReactFlow,
  type EdgeProps,
} from "@xyflow/react";
import { RotateCcwIcon } from "lucide-react";

import type { AgentTeamEdgeRoute, TeamGraphEdge, TeamGraphNode } from "@/components/agent-team-graph-model";

type RouteEditActions = {
  preview: (routeKey: string, route: AgentTeamEdgeRoute) => void;
  commit: (routeKey: string, route: AgentTeamEdgeRoute, coalesceKey?: string) => void;
  cancel: () => void;
  reset: (routeKey: string) => void;
};

export const AgentTeamEdgeRouteContext = createContext<RouteEditActions | null>(null);

type EdgeDrag = {
  pointerId: number;
  routeKey: string;
  start: { x: number; y: number };
  axis: AgentTeamEdgeRoute["axis"] | null;
  changed: boolean;
};

function useRouteEditActions(): RouteEditActions {
  const actions = useContext(AgentTeamEdgeRouteContext);
  if (!actions) throw new Error("Agent Team 连线编辑器缺少路径编辑上下文");
  return actions;
}

function createManualPath(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
  route: AgentTeamEdgeRoute,
): { path: string; control: { x: number; y: number }; label: { x: number; y: number } } {
  if (route.axis === "x") {
    const laneX = (sourceX + targetX) / 2 + route.offset;
    const sourceSegmentLength = Math.abs(laneX - sourceX);
    const targetSegmentLength = Math.abs(targetX - laneX);
    return {
      path: "M " + sourceX + " " + sourceY + " H " + laneX + " V " + targetY + " H " + targetX,
      control: { x: laneX, y: (sourceY + targetY) / 2 },
      label: sourceSegmentLength >= targetSegmentLength
        ? { x: (sourceX + laneX) / 2, y: sourceY }
        : { x: (laneX + targetX) / 2, y: targetY },
    };
  }

  const direction = targetX >= sourceX ? 1 : -1;
  const lead = Math.min(20, Math.abs(targetX - sourceX) / 3);
  const sourceLeadX = sourceX + lead * direction;
  const targetLeadX = targetX - lead * direction;
  const laneY = (sourceY + targetY) / 2 + route.offset;
  return {
    path: "M " + sourceX + " " + sourceY + " H " + sourceLeadX
      + " V " + laneY + " H " + targetLeadX + " V " + targetY + " H " + targetX,
    control: { x: (sourceLeadX + targetLeadX) / 2, y: laneY },
    label: Math.abs(laneY - sourceY) >= Math.abs(targetY - laneY)
      ? { x: sourceLeadX, y: (sourceY + laneY) / 2 }
      : { x: targetLeadX, y: (targetY + laneY) / 2 },
  };
}

export function AgentTeamRoutedEdge(props: EdgeProps<TeamGraphEdge>) {
  const {
    id,
    data,
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    selected,
    markerEnd,
    markerStart,
    style,
    label,
    labelStyle,
    labelShowBg,
    labelBgStyle,
    labelBgPadding,
    labelBgBorderRadius,
    interactionWidth,
  } = props;
  if (!data) throw new Error("Agent Team 连线缺少状态和路径标识");
  const edgeData = data;
  const flow = useReactFlow<TeamGraphNode>();
  const actions = useRouteEditActions();
  const dragRef = useRef<EdgeDrag | null>(null);
  const fallback = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });
  const route = edgeData.route;
  const manual = route ? createManualPath(sourceX, sourceY, targetX, targetY, route) : null;
  const path = manual?.path ?? fallback[0];
  const labelX = manual?.label.x ?? fallback[1];
  const labelY = manual?.label.y ?? fallback[2];
  const controlX = manual?.control.x ?? fallback[1];
  const controlY = manual?.control.y ?? fallback[2];
  const midpointX = (sourceX + targetX) / 2;
  const midpointY = (sourceY + targetY) / 2;

  const updateFromPointer = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const point = flow.screenToFlowPosition({ x: event.clientX, y: event.clientY });
    if (!drag.axis) {
      const deltaX = point.x - drag.start.x;
      const deltaY = point.y - drag.start.y;
      if (Math.max(Math.abs(deltaX), Math.abs(deltaY)) < 4) return;
      drag.axis = Math.abs(deltaX) >= Math.abs(deltaY) ? "x" : "y";
    }
    const nextRoute: AgentTeamEdgeRoute = {
      axis: drag.axis,
      offset: drag.axis === "x" ? point.x - midpointX : point.y - midpointY,
    };
    drag.changed = true;
    actions.preview(drag.routeKey, nextRoute);
  };

  const finishPointer = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    updateFromPointer(event);
    dragRef.current = null;
    if (drag.changed && drag.axis) {
      const point = flow.screenToFlowPosition({ x: event.clientX, y: event.clientY });
      actions.commit(drag.routeKey, {
        axis: drag.axis,
        offset: drag.axis === "x" ? point.x - midpointX : point.y - midpointY,
      });
    } else {
      actions.cancel();
    }
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      routeKey: edgeData.routeKey,
      start: flow.screenToFlowPosition({ x: event.clientX, y: event.clientY }),
      axis: null,
      changed: false,
    };
  };

  const onPointerCancel = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    actions.cancel();
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const keyAxis = event.key === "ArrowLeft" || event.key === "ArrowRight"
      ? "x"
      : event.key === "ArrowUp" || event.key === "ArrowDown"
        ? "y"
        : null;
    if (!keyAxis) return;
    event.preventDefault();
    event.stopPropagation();
    const direction = event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1;
    const currentOffset = route?.axis === keyAxis ? route.offset : 0;
    actions.commit(edgeData.routeKey, { axis: keyAxis, offset: currentOffset + direction * 8 }, "keyboard-edge-route:" + edgeData.routeKey);
  };

  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        markerStart={markerStart}
        markerEnd={markerEnd}
        style={style}
        interactionWidth={interactionWidth}
        label={label}
        labelX={labelX}
        labelY={labelY}
        labelStyle={labelStyle}
        labelShowBg={labelShowBg}
        labelBgStyle={labelBgStyle}
        labelBgPadding={labelBgPadding}
        labelBgBorderRadius={labelBgBorderRadius}
      />
      {selected && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan absolute z-10"
            style={{
              left: controlX,
              top: controlY,
              transform: "translate(-50%, -50%)",
              pointerEvents: "all",
            }}
          >
            <button
              type="button"
              aria-label={"拖动 " + edgeData.status + " 连线调整路径；方向键可微调"}
              title="拖动调整路径 · 方向键微调"
              className="border-background bg-info size-3 cursor-grab touch-none rounded-full border-2 shadow-sm focus-ring active:cursor-grabbing"
              onPointerDown={onPointerDown}
              onPointerMove={updateFromPointer}
              onPointerUp={finishPointer}
              onPointerCancel={onPointerCancel}
              onLostPointerCapture={onPointerCancel}
              onKeyDown={onKeyDown}
              onClick={(event) => event.stopPropagation()}
            />
            {route && (
              <button
                type="button"
                aria-label={"重置 " + edgeData.status + " 连线路径"}
                title="恢复自动路径"
                className="border-background bg-card text-muted-foreground hover:text-foreground absolute -right-5 -top-5 inline-flex size-5 items-center justify-center rounded-full border shadow-sm focus-ring"
                onPointerDown={(event) => event.stopPropagation()}
                onClick={(event) => {
                  event.stopPropagation();
                  actions.reset(edgeData.routeKey);
                }}
              >
                <RotateCcwIcon className="size-3" />
              </button>
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
