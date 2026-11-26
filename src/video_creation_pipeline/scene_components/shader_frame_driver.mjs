/** Host-clock glue only. No renderer import, network, RAF, media I/O or GPU creation.
 * The caller supplies a separately audited renderer and a local static frame.
 * Tests exercise the pinned upstream preset wrapper with a synthetic GPU backend.
 */
const activeNamespaces = new Set();
const usedRenderers = new WeakSet();

function positiveInteger(value, name) {
  if (!Number.isSafeInteger(value) || value <= 0) throw new TypeError(`${name} must be a positive safe integer`);
}

function isolatedPreset(preset, prefix) {
  const copy = structuredClone(preset);
  if (!Array.isArray(copy?.components)) throw new TypeError('preset.components required');
  const ids = new Map();
  const visit = (nodes, action) => {
    for (const node of nodes) {
      action(node);
      if (node.children) visit(node.children, action);
    }
  };
  visit(copy.components, node => {
    if (typeof node.id !== 'string' || !node.id || ids.has(node.id)) throw new TypeError('explicit unique node IDs required');
    ids.set(node.id, `${prefix}/${node.id}`);
  });
  visit(copy.components, node => {
    node.id = ids.get(node.id);
    if (node.props?.maskSource) {
      if (!ids.has(node.props.maskSource)) throw new TypeError('mask must reference a node in this preset');
      node.props.maskSource = ids.get(node.props.maskSource);
    }
  });
  return copy;
}

export function createShaderFrameDriver({createRenderer, preset, canvas, fps, namespace,
  staticFrame, reducedMotion = false, maxFrame = 1800}) {
  positiveInteger(fps?.numerator, 'fps.numerator');
  positiveInteger(fps?.denominator, 'fps.denominator');
  positiveInteger(maxFrame, 'maxFrame');
  if (!Number.isSafeInteger(maxFrame * fps.denominator)) throw new TypeError('frame time exceeds integer range');
  if (typeof namespace !== 'string' || !/^[A-Za-z0-9_.-]{1,100}$/.test(namespace)) throw new TypeError('stable job/scene namespace required');
  if (staticFrame == null) throw new TypeError('local static fallback required');
  if (typeof createRenderer !== 'function') throw new TypeError('renderer factory required');
  const source = structuredClone(preset);
  isolatedPreset(source, namespace); // Validate before reserving the namespace.
  if (activeNamespaces.has(namespace)) throw new Error('namespace already active');
  activeNamespaces.add(namespace);
  let renderer = null, generation = 0, frame = -1, paused = false, busy = false;
  let disposed = false, failure = reducedMotion ? 'reduced_motion' : null;
  const snapshot = () => {
    const reason = disposed ? 'disposed' : failure || (frame < 0 ? 'not_started' : null);
    return {mode: reason ? 'static' : 'dynamic', frame,
      time: {numerator: Math.max(0, frame) * fps.denominator, denominator: fps.numerator},
      paused, reason, surface: reason ? staticFrame : canvas};
  };
  function release() {
    const old = renderer; renderer = null;
    if (old) { old.setOnUnavailable(null); old.dispose(); }
  }
  function fallback(reason) { failure = String(reason || 'renderer_unavailable'); release(); }
  async function start() {
    const next = createRenderer(isolatedPreset(source, `${namespace}/run-${generation++}`));
    if (!next || usedRenderers.has(next)) throw new Error('factory must return a fresh renderer for every replay');
    usedRenderers.add(next); renderer = next;
    next.setOnUnavailable(reason => fallback(reason));
    await next.initialize(canvas);
    if (failure) return;
    const reason = next.getFailureReason();
    if (reason) { fallback(reason); return; }
    await next.renderFrame({deltaSeconds: 0, waitForGpu: true});
    if (!failure) frame = 0;
  }
  return {
    async drawFrame(target) {
      if (disposed) throw new Error('driver disposed');
      if (!Number.isSafeInteger(target) || target < 0 || target > maxFrame) throw new RangeError('frame outside finite replay budget');
      if (busy) throw new Error('concurrent draw is forbidden');
      if (paused || failure) return snapshot();
      busy = true;
      try {
        if (target < frame) { release(); frame = -1; }
        if (!renderer) await start();
        // A direct seek still replays each intervening simulation step. No target-time delta.
        while (!failure && frame < target) {
          await renderer.renderFrame({deltaSeconds: fps.denominator / fps.numerator, waitForGpu: true});
          if (!failure) frame += 1;
        }
        return snapshot();
      } catch (error) {
        // Author/configuration errors remain visible; leave the supplied static frame usable.
        fallback(`render_error: ${error.message}`);
        throw error;
      } finally { busy = false; }
    },
    pause() { if (disposed) throw new Error('driver disposed'); if (busy) throw new Error('pause between frames only'); paused = true; return snapshot(); },
    resume() { if (disposed) throw new Error('driver disposed'); paused = false; },
    state: snapshot,
    dispose() {
      if (busy) throw new Error('dispose between frames only');
      if (!disposed) { try { release(); } finally { activeNamespaces.delete(namespace); disposed = true; } }
    },
  };
}
