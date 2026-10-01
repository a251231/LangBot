/**
 * Pure geometry + state helpers for the floating workspace assistant button.
 *
 * The component keeps these decisions here so the dock/rail transitions stay
 * testable without a DOM. The key invariant is that the *same* edge value
 * drives the collapsed strip and the expanded button, so they can never drift
 * apart or fight each other (the race the UI is careful to avoid).
 */

export const ASSISTANT_BUTTON_SIZE = 48;
/** Distance to the viewport edge that snaps the button into the rail. */
export const ASSISTANT_SNAP_THRESHOLD = 24;
/** Rail strip that stays visible while collapsed, in pixels. */
export const ASSISTANT_RAIL_WIDTH = 6;

export type AssistantDragPosition = { x: number; y: number };
export type AssistantEdge = 'left' | 'right' | null;

/** Viewport box the button may occupy. Kept explicit so tests can pin a size. */
export type AssistantViewport = { width: number; height: number };

export function clampAssistantPosition(
  x: number,
  y: number,
  viewport: AssistantViewport,
): AssistantDragPosition {
  const maxX = Math.max(0, viewport.width - ASSISTANT_BUTTON_SIZE);
  const maxY = Math.max(0, viewport.height - ASSISTANT_BUTTON_SIZE);
  return {
    x: Math.min(Math.max(0, x), maxX),
    y: Math.min(Math.max(0, y), maxY),
  };
}

/**
 * Decide whether a resting position is docked against a vertical edge.
 * The left edge wins ties so a centred drag always resolves deterministically.
 */
export function resolveAssistantEdge(
  x: number,
  viewport: AssistantViewport,
): AssistantEdge {
  const maxX = Math.max(0, viewport.width - ASSISTANT_BUTTON_SIZE);
  const leftGap = x;
  const rightGap = maxX - x;
  if (leftGap <= ASSISTANT_SNAP_THRESHOLD && leftGap <= rightGap) return 'left';
  if (rightGap <= ASSISTANT_SNAP_THRESHOLD) return 'right';
  return null;
}

export type AssistantHoverInput = {
  dockedEdge: AssistantEdge;
  railExpanded: boolean;
  /** A long press has armed the drag, or the pointer is actively moving. */
  dragging: boolean;
};

/** Distance to the viewport edge that snaps the button into the rail. */

/**
 * Hovering any part of the control must reveal the full button again.
 * Only a docked *and still collapsed* button needs to expand, so a pointer that
 * merely jitters inside an already expanded button does no state churn.
 */
export function shouldExpandRail(input: AssistantHoverInput): boolean {
  if (input.dragging) return false;
  return !!input.dockedEdge && !input.railExpanded;
}

/**
 * A docked button collapses to the strip when the pointer leaves. This is the
 * mirror of `shouldExpandRail` and is keyed on `railExpanded` being *true* —
 * the earlier `!railExpanded` form made a revealed button impossible to
 * re-collapse, which is the bug this guards against. The panel being open pins
 * it, because the popover stays anchored to the button.
 */
export function shouldCollapseRail(input: AssistantHoverInput): boolean {
  if (input.dragging) return false;
  return !!input.dockedEdge && input.railExpanded;
}

/**
 * After a drop the button always starts collapsed, even though the pointer is
 * still over it. The rail is then held collapsed until the pointer exits, which
 * avoids the "drops under the cursor and never comes back" race.
 */
export function restingRailExpanded(): boolean {
  return false;
}
