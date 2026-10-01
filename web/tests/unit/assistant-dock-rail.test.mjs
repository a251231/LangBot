import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

// The floating assistant button docks against a viewport edge, collapses into a
// short blue rail, and re-expands on hover. The transition rules are pure so
// they can be pinned here: the race this guards against is a button that drops
// under the cursor and never becomes visible again.
const sourcePath = new URL(
  '../../src/app/home/components/assistant-dock.ts',
  import.meta.url,
);
assert.ok(fs.existsSync(sourcePath), 'Missing assistant dock module');
const compiled = ts.transpileModule(fs.readFileSync(sourcePath, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;
const loaded = { exports: {} };
new Function('require', 'module', 'exports', compiled)(
  () => {
    throw new Error('assistant-dock must stay dependency free');
  },
  loaded,
  loaded.exports,
);
const {
  ASSISTANT_BUTTON_SIZE,
  ASSISTANT_SNAP_THRESHOLD,
  ASSISTANT_RAIL_WIDTH,
  clampAssistantPosition,
  resolveAssistantEdge,
  shouldCollapseRail,
  shouldExpandRail,
  restingRailExpanded,
} = loaded.exports;

const viewport = { width: 1280, height: 800 };
const maxX = viewport.width - ASSISTANT_BUTTON_SIZE;

test('clamping keeps the button fully inside the viewport', () => {
  assert.deepEqual(clampAssistantPosition(-50, -50, viewport), { x: 0, y: 0 });
  assert.deepEqual(clampAssistantPosition(9999, 9999, viewport), {
    x: maxX,
    y: viewport.height - ASSISTANT_BUTTON_SIZE,
  });
  assert.deepEqual(clampAssistantPosition(400, 300, viewport), {
    x: 400,
    y: 300,
  });
});

test('a narrow viewport never produces a negative travel range', () => {
  assert.deepEqual(clampAssistantPosition(10, 10, { width: 20, height: 20 }), {
    x: 0,
    y: 0,
  });
});

test('positions within the snap threshold dock to the nearest edge', () => {
  assert.equal(resolveAssistantEdge(0, viewport), 'left');
  assert.equal(
    resolveAssistantEdge(ASSISTANT_SNAP_THRESHOLD, viewport),
    'left',
  );
  assert.equal(resolveAssistantEdge(maxX, viewport), 'right');
  assert.equal(
    resolveAssistantEdge(maxX - ASSISTANT_SNAP_THRESHOLD, viewport),
    'right',
  );
});

test('a centred position stays free floating', () => {
  assert.equal(resolveAssistantEdge(Math.round(maxX / 2), viewport), null);
  assert.equal(
    resolveAssistantEdge(ASSISTANT_SNAP_THRESHOLD + 1, viewport),
    null,
  );
});

test('the left edge wins ties so a centred drop is deterministic', () => {
  // A viewport sized so both gaps are inside the snap threshold at x = 0.
  const tight = { width: ASSISTANT_BUTTON_SIZE, height: 400 };
  assert.equal(resolveAssistantEdge(0, tight), 'left');
});

test('hovering a docked rail expands it, hovering a free button does not', () => {
  assert.equal(
    shouldExpandRail({
      dockedEdge: 'right',
      railExpanded: false,
      dragging: false,
    }),
    true,
  );
  assert.equal(
    shouldExpandRail({
      dockedEdge: null,
      railExpanded: false,
      dragging: false,
    }),
    false,
  );
  // Already expanded: nothing to do, so no redundant state churn on every move.
  assert.equal(
    shouldExpandRail({
      dockedEdge: 'left',
      railExpanded: true,
      dragging: false,
    }),
    false,
  );
});

test('a drag in progress never expands or collapses the rail', () => {
  const dragging = { dockedEdge: 'left', railExpanded: false, dragging: true };
  assert.equal(shouldExpandRail(dragging), false);
  assert.equal(shouldCollapseRail(dragging), false);
});

test('only an *expanded* docked button collapses when the pointer leaves', () => {
  // Regression: keying this on `!railExpanded` made a revealed button
  // impossible to collapse again, so it stayed open forever after one hover.
  assert.equal(
    shouldCollapseRail({
      dockedEdge: 'left',
      railExpanded: true,
      dragging: false,
    }),
    true,
  );
  assert.equal(
    shouldCollapseRail({
      dockedEdge: 'left',
      railExpanded: false,
      dragging: false,
    }),
    false,
  );
  assert.equal(
    shouldCollapseRail({
      dockedEdge: null,
      railExpanded: true,
      dragging: false,
    }),
    false,
  );
});

test('expand then leave round-trips back to the collapsed rail', () => {
  const docked = { dockedEdge: 'right', railExpanded: false, dragging: false };
  // Hover: reveal the button.
  assert.equal(shouldExpandRail(docked), true);
  // Pointer leaves: the revealed button must collapse again.
  assert.equal(shouldCollapseRail({ ...docked, railExpanded: true }), true);
  // A second hover re-reveals it, so the cycle is repeatable.
  assert.equal(shouldExpandRail(docked), true);
});

test('dropping always starts collapsed so the rail is never stuck open', () => {
  // The pointer is still over the button on drop; the next pointermove expands.
  assert.equal(restingRailExpanded(), false);
  assert.equal(
    shouldExpandRail({
      dockedEdge: 'right',
      railExpanded: restingRailExpanded(),
      dragging: false,
    }),
    true,
  );
});

test('the rail strip stays thinner than the button it replaces', () => {
  assert.ok(ASSISTANT_RAIL_WIDTH > 0);
  assert.ok(ASSISTANT_RAIL_WIDTH < ASSISTANT_BUTTON_SIZE);
});
