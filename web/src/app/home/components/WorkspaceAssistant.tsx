import {
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { MessageCircle, Plus, Send, X, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { backendClient, useCurrentWorkspace, userInfo } from '@/app/infra/http';
import { Button } from '@/components/ui/button';
import DynamicFormItemComponent from './dynamic-form/DynamicFormItemComponent';
import { DynamicFormItemType } from '@/app/infra/entities/form/dynamic';
import AssistantToolResult, { AssistantTool } from './AssistantToolResult';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import {
  ASSISTANT_RAIL_WIDTH,
  clampAssistantPosition as clampInViewport,
  resolveAssistantEdge,
  shouldCollapseRail,
  shouldExpandRail,
  type AssistantDragPosition,
  type AssistantEdge,
} from './assistant-dock';

const ASSISTANT_LONG_PRESS_MS = 260;

function viewport(): { width: number; height: number } {
  return { width: window.innerWidth, height: window.innerHeight };
}

function clampAssistantPosition(x: number, y: number): AssistantDragPosition {
  return clampInViewport(x, y, viewport());
}

type Conversation = {
  uuid: string;
  revision: number;
  status: 'ready' | 'running' | 'approval' | 'failed';
  messages: { role: string; content: string; tool?: AssistantTool }[];
  pending: { name: string; arguments: Record<string, unknown> }[];
  error: string | null;
  model_name: string | null;
  model_uuid: string | null;
};

export default function WorkspaceAssistant() {
  const workspace = useCurrentWorkspace();
  if (
    !workspace?.permissions.includes('runtime.operate') ||
    !userInfo?.account_uuid
  )
    return null;
  const identity = `${workspace.workspace.uuid}:${userInfo.account_uuid}`;
  return (
    <AssistantPanel
      key={identity}
      storageKey={`langbot-assistant:${identity}`}
    />
  );
}

function AssistantPanel({ storageKey }: { storageKey: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [text, setText] = useState('');
  const [sending, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const busy = sending || loading;
  const [error, setError] = useState(false);
  const [modelUuid, setModelUuid] = useState('');
  const [pendingText, setPendingText] = useState<string | null>(null);
  const controller = useRef(new AbortController());
  const end = useRef<HTMLDivElement>(null);

  const [dragPosition, setDragPosition] =
    useState<AssistantDragPosition | null>(null);
  const [dragging, setDragging] = useState(false);
  const [dockedEdge, setDockedEdge] = useState<AssistantEdge>(null);
  // The rail collapses only while the pointer is away. Hovering any part of the
  // control restores the full button, and the two states never race because
  // every transition is derived from the same `dockedEdge` snapshot.
  const [railExpanded, setRailExpanded] = useState(false);
  const pointerOver = useRef(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const hoverLocked = useRef(false);
  const dragState = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
    active: boolean;
  } | null>(null);
  const longPressTimer = useRef<number | null>(null);
  const suppressClick = useRef(false);
  const buttonRef = useRef<HTMLButtonElement>(null);

  function clearLongPressTimer() {
    if (longPressTimer.current !== null) {
      window.clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  }

  useEffect(() => clearLongPressTimer, []);

  /*
   * Seed the resting position from the rendered default (bottom-right) so the
   * very first visit already docks and collapses. Without this the button would
   * only ever dock after a manual drag, which reads as "collapse is broken".
   */
  useEffect(() => {
    if (window.localStorage.getItem(`${storageKey}:button-position`)) return;
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    const position = clampAssistantPosition(rect.left, rect.top);
    setDragPosition(position);
    const edge = resolveAssistantEdge(position.x, viewport());
    setDockedEdge(edge);
    if (edge) {
      try {
        window.localStorage.setItem(`${storageKey}:button-docked-edge`, edge);
      } catch {
        // Persisting the dock is best-effort only.
      }
    }
  }, [storageKey]);

  useEffect(() => {
    const stored = window.localStorage.getItem(`${storageKey}:button-position`);
    if (stored) {
      try {
        const parsed = JSON.parse(stored) as AssistantDragPosition;
        if (typeof parsed?.x === 'number' && typeof parsed?.y === 'number') {
          const position = clampAssistantPosition(parsed.x, parsed.y);
          setDragPosition(position);
          setDockedEdge(resolveAssistantEdge(position.x, viewport()));
        }
      } catch {
        window.localStorage.removeItem(`${storageKey}:button-position`);
      }
    }
    const storedEdge = window.localStorage.getItem(
      `${storageKey}:button-docked-edge`,
    );
    if (storedEdge === 'left' || storedEdge === 'right') {
      setDockedEdge((current) => current ?? storedEdge);
    }
  }, [storageKey]);

  useEffect(() => {
    const onResize = () => {
      setDragPosition((prev) => {
        if (!prev) return prev;
        const next = clampAssistantPosition(prev.x, prev.y);
        setDockedEdge(resolveAssistantEdge(next.x, viewport()));
        return next;
      });
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  function persistDragPosition(position: AssistantDragPosition) {
    try {
      window.localStorage.setItem(
        `${storageKey}:button-position`,
        JSON.stringify(position),
      );
    } catch {
      // Ignore storage failures (private mode or quota); dragging still works.
    }
  }

  function applyRestingPosition(position: AssistantDragPosition) {
    const edge = resolveAssistantEdge(position.x, viewport());
    setDragPosition(position);
    setDockedEdge(edge);
    // A fresh dock always collapses; the rail re-expands on the next hover.
    setRailExpanded(false);
    // Hold the collapse until the pointer leaves, otherwise the still-hovering
    // cursor would fight the new state.
    if (edge) lockHoverUntilPointerExit();
    try {
      if (edge) {
        window.localStorage.setItem(`${storageKey}:button-docked-edge`, edge);
      } else {
        window.localStorage.removeItem(`${storageKey}:button-docked-edge`);
      }
    } catch {
      // Persisting the dock is best-effort only.
    }
  }

  function endDrag(commit: boolean, clientX?: number, clientY?: number) {
    const state = dragState.current;
    clearLongPressTimer();
    dragState.current = null;
    if (!state?.active) return;
    setDragging(false);
    const next = clampAssistantPosition(
      state.originX + ((clientX ?? state.startX) - state.startX),
      state.originY + ((clientY ?? state.startY) - state.startY),
    );
    if (commit) {
      applyRestingPosition(next);
      persistDragPosition(next);
    } else {
      setDragPosition(next);
    }
  }

  /*
   * Rail hover recovery. The subtle race: a drag usually ends with the pointer
   * still sitting on the button, so the browser fires no new `pointerenter`
   * once the button collapses. Re-expanding on `pointermove` would therefore
   * undo the collapse immediately.
   *
   * Instead the drop "locks" hover until the pointer physically leaves the
   * control. A window-level move listener watches for that exit (the element
   * can shift under a stationary cursor, so `pointerleave` alone is not
   * reliable) and clears the lock; only then does hovering reveal the button.
   */
  function handlePointerEnter() {
    pointerOver.current = true;
    if (hoverLocked.current) return;
    if (shouldExpandRail({ dockedEdge, railExpanded, dragging }))
      setRailExpanded(true);
  }

  function handlePointerLeave() {
    pointerOver.current = false;
    hoverLocked.current = false;
    // Never collapse mid-drag; the drop handler owns the final state.
    if (dragState.current || dragging) return;
    if (shouldCollapseRail({ dockedEdge, railExpanded, dragging }))
      setRailExpanded(false);
  }

  /*
   * A click or an aborted swipe never arms the long press, so no drop handler
   * runs. Re-sync the rail from the real pointer position on release, otherwise
   * a button expanded by hover would stay expanded with the cursor gone.
   */
  function settleRailAfterRelease() {
    if (pointerOver.current || dragState.current || dragging) return;
    if (shouldCollapseRail({ dockedEdge, railExpanded, dragging }))
      setRailExpanded(false);
  }

  function lockHoverUntilPointerExit() {
    hoverLocked.current = true;
    const releaseOnExit = (moveEvent: PointerEvent) => {
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const outside =
        moveEvent.clientX < rect.left ||
        moveEvent.clientX > rect.right ||
        moveEvent.clientY < rect.top ||
        moveEvent.clientY > rect.bottom;
      if (!outside) return;
      hoverLocked.current = false;
      window.removeEventListener('pointermove', releaseOnExit);
    };
    window.addEventListener('pointermove', releaseOnExit);
  }

  function onButtonPointerDown(event: ReactPointerEvent<HTMLButtonElement>) {
    if (event.button !== 0) return;
    suppressClick.current = false;
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    const state = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: rect.left,
      originY: rect.top,
      active: false,
    };
    dragState.current = state;
    clearLongPressTimer();

    const onWindowMove = (moveEvent: PointerEvent) => {
      if (moveEvent.pointerId !== state.pointerId) return;
      const deltaX = moveEvent.clientX - state.startX;
      const deltaY = moveEvent.clientY - state.startY;

      if (!state.active) {
        // Cancel the long-press when the user is clearly scrolling or swiping.
        if (Math.hypot(deltaX, deltaY) > 8) clearLongPressTimer();
        return;
      }

      moveEvent.preventDefault();
      setDragPosition(
        clampAssistantPosition(state.originX + deltaX, state.originY + deltaY),
      );
    };

    const onWindowUp = (upEvent: PointerEvent) => {
      if (upEvent.pointerId !== state.pointerId) return;
      window.removeEventListener('pointermove', onWindowMove);
      window.removeEventListener('pointerup', onWindowUp);
      window.removeEventListener('pointercancel', onWindowUp);
      endDrag(true, upEvent.clientX, upEvent.clientY);
    };

    window.addEventListener('pointermove', onWindowMove, { passive: false });
    window.addEventListener('pointerup', onWindowUp);
    window.addEventListener('pointercancel', onWindowUp);

    // Release outside a drag still needs to reconcile the rail.
    window.addEventListener('pointerup', settleRailAfterRelease, {
      once: true,
    });

    longPressTimer.current = window.setTimeout(() => {
      if (dragState.current !== state) return;
      state.active = true;
      setDragging(true);
      suppressClick.current = true;
      setDragPosition(clampAssistantPosition(state.originX, state.originY));
    }, ASSISTANT_LONG_PRESS_MS);
  }

  function onButtonClick(event: ReactMouseEvent<HTMLButtonElement>) {
    // After a drag the trailing click must not toggle the panel. Radix's
    // trigger skips its own toggle when the event default is prevented.
    if (suppressClick.current) {
      suppressClick.current = false;
      event.preventDefault();
      event.stopPropagation();
    }
  }

  useEffect(() => {
    if (conversation?.model_uuid) setModelUuid(conversation.model_uuid);
  }, [conversation?.model_uuid]);

  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    return () => abort.abort();
  }, []);

  useEffect(() => {
    if (!open || busy || (conversation && conversation.status !== 'running'))
      return;
    const id = localStorage.getItem(storageKey);
    if (!id) return;
    let active = true;
    setLoading(true);
    backendClient
      .request<Conversation>({
        method: 'GET',
        url: `/api/v1/assistant/conversations/${encodeURIComponent(id)}`,
        signal: controller.current.signal,
      })
      .then((value) => {
        if (active) setConversation(value);
      })
      .catch(() => {
        if (active) {
          localStorage.removeItem(storageKey);
          setError(true);
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      setLoading(false);
    };
    // Load only when opening; turn requests own subsequent state updates.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, storageKey]);

  // `open` is a dependency so a freshly opened panel jumps to the newest turn
  // instead of leaving the user at the oldest message. The rAF waits for the
  // popover to lay out before measuring the sentinel.
  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      end.current?.scrollIntoView({ behavior: 'auto', block: 'nearest' });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open, conversation, busy, pendingText]);

  async function submit(approved?: boolean) {
    if (
      busy ||
      (approved === undefined &&
        (!text.trim() || (conversation && conversation.status !== 'ready')))
    )
      return;
    const sentText = approved === undefined ? text.trim() : null;
    const sentRevision = conversation?.revision ?? 0;
    if (sentText) {
      setPendingText(sentText);
      setText('');
    }
    setBusy(true);
    setError(false);
    try {
      let current = conversation;
      if (!current) {
        current = await backendClient.request<Conversation>({
          method: 'POST',
          url: '/api/v1/assistant/conversations',
          signal: controller.current.signal,
        });
        localStorage.setItem(storageKey, current.uuid);
        setConversation(current);
      }
      const updated = await backendClient.request<Conversation>({
        method: 'POST',
        url: `/api/v1/assistant/conversations/${current.uuid}/turn`,
        data: {
          revision: current.revision,
          ...(approved === undefined
            ? {
                text: sentText,
                ...(modelUuid ? { model_uuid: modelUuid } : {}),
              }
            : { approved }),
        },
        timeout: 130000,
        signal: controller.current.signal,
      });
      setConversation(updated);
      if (sentText) setPendingText(null);
    } catch {
      setError(true);
      // A lost response may already have executed a write. Refresh, never replay.
      const id = localStorage.getItem(storageKey);
      if (id && !controller.current.signal.aborted) {
        try {
          const latest = await backendClient.request<Conversation>({
            method: 'GET',
            url: `/api/v1/assistant/conversations/${encodeURIComponent(id)}`,
            signal: controller.current.signal,
          });
          setConversation(latest);
          if (
            sentText &&
            latest.revision > sentRevision &&
            latest.messages.some(
              (message) =>
                message.role === 'user' && message.content === sentText,
            )
          )
            setPendingText(null);
        } catch {
          /* Keep the error visible; do not retry a turn. */
        }
      }
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    localStorage.removeItem(storageKey);
    setConversation(null);
    setText('');
    setError(false);
    setPendingText(null);
  }

  // Collapse only when docked, idle and not hovered. The container keeps its
  // resting box, so the hidden button and the visible strip share one anchor
  // and cannot drift apart; only the strip is painted while collapsed.
  const railCollapsed = !!dockedEdge && !railExpanded && !dragging;
  const inlinePosition = dragPosition
    ? { left: dragPosition.x, top: dragPosition.y }
    : undefined;

  return (
    <div
      ref={containerRef}
      className={dragPosition ? 'fixed z-50' : 'fixed bottom-20 right-5 z-50'}
      style={inlinePosition}
      onPointerEnter={handlePointerEnter}
      onPointerLeave={handlePointerLeave}
    >
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            ref={buttonRef}
            size="icon"
            aria-label={t('assistant.title')}
            aria-expanded={railCollapsed ? false : undefined}
            className={`size-12 rounded-full shadow-lg ${
              dragging
                ? 'cursor-grabbing scale-105'
                : railCollapsed
                  ? 'cursor-pointer'
                  : 'cursor-grab'
            } transition-[transform,opacity] duration-200`}
            style={{
              touchAction: 'none',
              opacity: railCollapsed ? 0 : 1,
            }}
            onClick={onButtonClick}
            onPointerDown={onButtonPointerDown}
            onContextMenu={(event) => event.preventDefault()}
          >
            <MessageCircle className="size-6" />
          </Button>
        </PopoverTrigger>
        {railCollapsed && (
          <span
            role="presentation"
            aria-hidden="true"
            className={`pointer-events-none absolute top-1/2 h-14 -translate-y-1/2 rounded-full bg-[#3b82f6] shadow-md ${
              dockedEdge === 'right' ? 'right-0' : 'left-0'
            }`}
            style={{ width: ASSISTANT_RAIL_WIDTH }}
          />
        )}
        <PopoverContent
          align="end"
          side="top"
          sideOffset={12}
          className="flex h-[min(680px,80dvh)] w-[min(440px,calc(100vw-24px))] flex-col overflow-hidden rounded-2xl p-0 shadow-xl"
          onOpenAutoFocus={(event) => event.preventDefault()}
        >
          <header className="flex items-center gap-2 border-b p-3">
            <MessageCircle className="size-5 text-primary" />
            <div className="min-w-0 flex-1">
              <h2 className="truncate font-semibold">{t('assistant.title')}</h2>
              <p className="truncate text-xs text-muted-foreground">
                {t('assistant.subtitle')}
              </p>
            </div>
            {open && (
              <div
                className="w-28 min-w-0 shrink-0"
                title={t('assistant.modelHint')}
              >
                <DynamicFormItemComponent
                  config={{
                    id: 'assistant-model',
                    name: 'assistant-model',
                    type: DynamicFormItemType.LLM_MODEL_SELECTOR,
                    default: '',
                    required: false,
                    label: { en_US: 'Assistant model', zh_Hans: '助手模型' },
                  }}
                  field={{
                    name: 'assistant-model',
                    value: modelUuid,
                    onChange: setModelUuid,
                    onBlur: () => {},
                    ref: () => {},
                    disabled:
                      busy ||
                      (!!conversation && conversation.status !== 'ready'),
                  }}
                  requiredModelAbility="func_call"
                  compactModelSelector
                />
              </div>
            )}
            <Button
              variant="ghost"
              size="icon"
              className="size-7 shrink-0"
              disabled={busy}
              onClick={reset}
              aria-label={t('assistant.newChat')}
            >
              <Plus />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="size-7 shrink-0"
              onClick={() => setOpen(false)}
              aria-label={t('assistant.close')}
            >
              <X />
            </Button>
          </header>
          <div
            className="flex-1 space-y-3 overflow-y-auto p-4"
            aria-live="polite"
          >
            {!conversation?.messages.length && !pendingText && (
              <>
                <p className="text-sm text-muted-foreground">
                  {t('assistant.welcome')}
                </p>
                {(['discover', 'build'] as const).map((key) => (
                  <button
                    key={key}
                    type="button"
                    disabled={busy}
                    className="w-full rounded-lg border p-3 text-left text-sm hover:bg-muted"
                    onClick={() => setText(t(`assistant.${key}`))}
                  >
                    {t(`assistant.${key}`)}
                  </button>
                ))}
              </>
            )}
            {conversation?.messages.map((message, index) =>
              message.role === 'tool' ? (
                <AssistantToolResult
                  key={index}
                  tool={message.tool}
                  content={message.content}
                  defaultCollapsed={!busy && conversation.status === 'ready'}
                />
              ) : (
                <div
                  key={index}
                  className={`rounded-xl p-3 text-sm break-words ${message.role === 'user' ? 'ml-6 bg-primary/10' : 'bg-muted'}`}
                >
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      img: () => null,
                      a: ({ href, children }) => (
                        <a
                          href={href}
                          className="text-primary underline"
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {children}
                        </a>
                      ),
                    }}
                  >
                    {message.content}
                  </ReactMarkdown>
                </div>
              ),
            )}
            {pendingText && (
              <div className="ml-6 rounded-xl bg-primary/10 p-3 text-sm whitespace-pre-wrap break-words">
                {pendingText}
                {error && (
                  <p className="mt-1 text-xs text-destructive">
                    {t('assistant.sendUnconfirmed')}
                  </p>
                )}
              </div>
            )}
            {conversation?.status === 'approval' && (
              <div className="space-y-3 rounded-xl border border-primary/30 p-3">
                <p className="text-sm font-medium">{t('assistant.review')}</p>
                {conversation.pending.map((call, index) => (
                  <div key={index}>
                    <p className="text-sm font-medium">{call.name}</p>
                    <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs">
                      {JSON.stringify(call.arguments, null, 2)}
                    </pre>
                  </div>
                ))}
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => submit(true)}
                  >
                    {t('assistant.confirm')}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() => submit(false)}
                  >
                    {t('assistant.decline')}
                  </Button>
                </div>
              </div>
            )}
            {busy && (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <LoaderCircle className="size-4 animate-spin" />
                {t('assistant.working')}
              </p>
            )}
            {(error || conversation?.status === 'failed') && (
              <p role="alert" className="text-sm text-destructive">
                {conversation?.error === 'model_unavailable'
                  ? t('assistant.modelUnavailable')
                  : t('assistant.error')}
              </p>
            )}
            {!busy && conversation?.status === 'running' && (
              <p className="text-sm text-muted-foreground">
                {t('assistant.running')}
              </p>
            )}
            <div ref={end} />
          </div>
          <form
            className="flex items-end gap-2 border-t p-3"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            <textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              maxLength={8000}
              rows={2}
              aria-label={t('assistant.placeholder')}
              placeholder={t(
                busy ? 'assistant.draftPlaceholder' : 'assistant.placeholder',
              )}
              onKeyDown={(event) => {
                if (
                  event.key === 'Enter' &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  void submit();
                }
              }}
              className="min-w-0 flex-1 resize-none rounded-lg border bg-background p-2 text-sm focus-visible:outline-2 focus-visible:outline-primary"
            />
            <Button
              type="submit"
              size="icon"
              aria-label={t('assistant.send')}
              disabled={
                busy ||
                !text.trim() ||
                (!!conversation && conversation.status !== 'ready')
              }
            >
              <Send className="size-4" />
            </Button>
          </form>
        </PopoverContent>
      </Popover>
    </div>
  );
}
