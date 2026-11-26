import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {createHash} from 'node:crypto';
import {stripTypeScriptTypes} from 'node:module';
import {createShaderFrameDriver} from '../src/video_creation_pipeline/scene_components/shader_frame_driver.mjs';

const review = process.env.SHADER_SOURCE_REVIEW_ROOT;
const pinned = {
  'presetRenderer.ts': '88ee437bfd63cae67bc9bbf373e7c8ade826cb1487539abda896ba15cd846cd0',
  'telemetry/collector.ts': '95148f72de627c4b68e5ffd9610f96658b40b2897073e0c6d3428ee21c83e499',
  'telemetry/config.ts': 'dc9c0ea949dc93409b580bc4475ddf81542dfc91676ac15cc8d16ab8ad7d0423',
  'telemetry/environmentCollector.ts': '4a72c99b16ebc9631416fabcaad10d60575f2feeb086b91c86f11a26a4c0fc6c',
};
function upstream(name, dependencies, exported) {
  assert.ok(review, 'SHADER_SOURCE_REVIEW_ROOT is required for pinned-source tests');
  const source = fs.readFileSync(path.join(review, 'upstream/shader-effects-inc/shaders/packages/core/src', name));
  assert.equal(createHash('sha256').update(source).digest('hex'), pinned[name], 'upstream source drift');
  // Only the selected wrapper/collector is executed; all imports are explicit in-memory substitutes.
  let code = stripTypeScriptTypes(source.toString('utf8'), {mode: 'strip'});
  code = code.replace(/^import .*?from ['"].*?['"];?\s*$/gm, '').replace(/^export /gm, '');
  const sandbox = {console: {warn() {}, error() {}}, debugWarn() {}, debugError() {},
    fetch() { throw new Error('network denied in offline test'); }, ...dependencies};
  const context = vm.createContext(sandbox, {codeGeneration: {strings: false, wasm: false}});
  new vm.Script(`${code}\nglobalThis.result = {${exported.join(',')}};`, {filename: name}).runInContext(context, {timeout: 1000});
  return context.result;
}
function backend({unavailable = false, failAt = null} = {}) {
  return {calls: [], nodes: [], initialized: false, locked: false, disposed: false,
    simulation: 0, callback: null, reason: null,
    async initialize() { this.initialized = !unavailable; if (unavailable) { this.reason = 'no_webgpu'; this.callback?.(this.reason); } },
    registerNode(...args) { this.nodes.push(args); }, stopAnimation() {}, removeNode() {},
    isInitialized() { return this.initialized; },
    setFrameLocked(value) { this.locked = value; },
    async renderSyntheticFrame(delta, options) {
      if (this.calls.length === failAt) { this.reason = 'device_lost'; this.callback?.(this.reason); return; }
      this.calls.push({delta, waitForGpu: options.waitForGpu}); this.simulation += delta * delta;
    },
    async renderAndWait() { this.wallClockCalls = (this.wallClockCalls || 0) + 1; },
    cleanup() { this.initialized = false; this.disposed = true; },
    setOnUnavailable(callback) { this.callback = callback; },
    getFailureReason() { return this.reason; }, getInternalRenderer() { return {}; },
  };
}
const preset = {components: [{type: 'Synthetic', id: 'subject', props: {}},
  {type: 'Synthetic', id: 'mask', props: {maskSource: 'subject'}}]};
function factory(options = {}) {
  const cores = [], presets = [];
  return {cores, presets, createRenderer(copy) {
    presets.push(copy); const core = backend(options); cores.push(core);
    core.setOnUnavailable = core.setOnUnavailable.bind(core);
    core.getFailureReason = core.getFailureReason.bind(core);
    const {createRendererFromJSON} = upstream('presetRenderer.ts', {
      shaderRendererGPU: () => core, rootPassthrough: {fragment: 'synthetic'},
      createGpuUniformsMap: () => ({}), getAllShaders: () => [{definition: {name: 'Synthetic', fragment: 'synthetic', props: {}}}],
      getRegisteredShader: () => undefined,
    }, ['createRendererFromJSON']);
    return createRendererFromJSON(copy);
  }};
}
function driver(f, namespace, extra = {}) {
  return createShaderFrameDriver({createRenderer: f.createRenderer, preset, canvas: {},
    staticFrame: {kind: 'synthetic-local-still'}, fps: {numerator: 60, denominator: 1}, namespace, ...extra});
}

test('cumulative simulation: seeking to 60 executes 60 steps, never a 1-second jump', async () => {
  const f = factory(), d = driver(f, 'cumulative');
  try {
    const state = await d.drawFrame(60), c = f.cores[0];
    assert.equal(c.calls.length, 61); assert.equal(c.calls[0].delta, 0);
    assert.ok(c.calls.slice(1).every(x => x.delta === 1 / 60 && x.waitForGpu === true));
    assert.ok(Math.abs(c.simulation - 1 / 60) < 1e-12);
    assert.notEqual(c.simulation, 1); // A time-only assertion would miss this bug.
    assert.deepEqual(state.time, {numerator: 60, denominator: 60});
  } finally { d.dispose(); }
});
test('pause and duplicate frame reuse previous output without rerunning simulation', async () => {
  const f = factory(), d = driver(f, 'pause');
  try {
    await d.drawFrame(3); d.pause(); await d.drawFrame(10);
    assert.equal(f.cores[0].calls.length, 4); assert.equal(d.state().frame, 3);
    d.resume(); await d.drawFrame(3); assert.equal(f.cores[0].calls.length, 4);
    await d.drawFrame(5); assert.equal(f.cores[0].calls.length, 6);
  } finally { d.dispose(); }
});
test('backward seek disposes and replays a fresh renderer from zero', async () => {
  const f = factory(), d = driver(f, 'rewind');
  try {
    await d.drawFrame(10); await d.drawFrame(4);
    assert.equal(f.cores.length, 2); assert.equal(f.cores[0].disposed, true);
    assert.equal(f.cores[1].calls.length, 5);
    assert.notEqual(f.presets[0].components[0].id, f.presets[1].components[0].id);
  } finally { d.dispose(); }
});
test('unavailable WebGPU selects supplied static frame and disposes', async () => {
  const f = factory({unavailable: true}), d = driver(f, 'unavailable');
  try {
    const state = await d.drawFrame(12);
    assert.equal(state.mode, 'static'); assert.equal(state.reason, 'no_webgpu');
    assert.equal(f.cores[0].calls.length, 0); assert.equal(f.cores[0].disposed, true);
    await d.drawFrame(15); assert.equal(f.cores.length, 1);
  } finally { d.dispose(); }
});
test('reduced motion never constructs a renderer', async () => {
  let created = 0;
  const d = driver({createRenderer() { created++; throw new Error('must not start'); }}, 'reduced', {reducedMotion: true});
  try { assert.equal((await d.drawFrame(30)).mode, 'static'); assert.equal(created, 0); }
  finally { d.dispose(); }
});
test('runtime unavailable callback stops subsequent frame progression', async () => {
  const f = factory({failAt: 3}), d = driver(f, 'lost');
  try { assert.equal((await d.drawFrame(20)).reason, 'device_lost'); assert.equal(f.cores[0].calls.length, 3); }
  finally { d.dispose(); }
});
test('state and node/mask IDs are isolated and input preset is immutable', async () => {
  const before = structuredClone(preset), f = factory(), a = driver(f, 'sceneA'), b = driver(f, 'sceneB');
  try {
    await a.drawFrame(4); await b.drawFrame(2);
    assert.equal(f.cores[0].calls.length, 5); assert.equal(f.cores[1].calls.length, 3);
    assert.notEqual(f.presets[0].components[0].id, f.presets[1].components[0].id);
    for (const p of f.presets) assert.equal(p.components[1].props.maskSource, p.components[0].id);
    assert.deepEqual(preset, before);
    assert.throws(() => driver(f, 'sceneA'), /already active/);
  } finally { a.dispose(); b.dispose(); }
});
test('rational clock, finite replay budget and concurrent draw rejection', async () => {
  const f = factory(), d = driver(f, 'clock', {fps: {numerator: 30000, denominator: 1001}, maxFrame: 40});
  try {
    const pending = d.drawFrame(30);
    await assert.rejects(d.drawFrame(31), /concurrent/);
    assert.deepEqual((await pending).time, {numerator: 30030, denominator: 30000});
    assert.ok(f.cores[0].calls.slice(1).every(x => x.delta === 1001 / 30000));
    for (const frame of [-1, 41, NaN, Infinity, 2.5]) await assert.rejects(d.drawFrame(frame), /budget/);
  } finally { d.dispose(); }
  await assert.rejects(d.drawFrame(0), /disposed/);
});
test('reused renderer on replay fails visibly and leaves static fallback', async () => {
  const f = factory(), cached = f.createRenderer(preset), d = driver({createRenderer: () => cached}, 'reuse');
  try {
    await d.drawFrame(5); await assert.rejects(d.drawFrame(2), /fresh renderer/);
    assert.equal(d.state().mode, 'static');
  } finally { d.dispose(); }
});
test('upstream wrapper rejects wall clock after frame lock and nonfinite deltas', async () => {
  const f = factory(), renderer = f.createRenderer(preset);
  await renderer.initialize({}); await renderer.renderFrame({deltaSeconds: 0});
  await assert.rejects(renderer.renderFrame(), /frame-locked/);
  await assert.rejects(renderer.renderFrame({deltaSeconds: NaN}), /finite/);
  assert.equal(f.cores[0].calls.length, 1); renderer.dispose();
});
test('upstream environment collector captures domain/browser/device (synthetic input)', () => {
  const {collectEnvironment} = upstream('telemetry/environmentCollector.ts', {
    window: {location: {hostname: 'synthetic.invalid'}}, navigator: {userAgent: 'Mozilla Chrome/123 Mobile Android'},
  }, ['collectEnvironment']);
  assert.deepEqual(JSON.parse(JSON.stringify(collectEnvironment())), {domain: 'synthetic.invalid', browserFamily: 'Chrome', deviceType: 'mobile'});
});
function telemetryHarness() {
  const {TELEMETRY_CONFIG} = upstream('telemetry/config.ts', {}, ['TELEMETRY_CONFIG']);
  const attempts = [];
  const {TelemetryCollector} = upstream('telemetry/collector.ts', {
    TELEMETRY_CONFIG: {...TELEMETRY_CONFIG, samplingRate: 0}, // Collector.start itself has no sampling gate.
    collectEnvironment: () => ({domain: 'synthetic.invalid', browserFamily: 'Synthetic', deviceType: 'synthetic'}),
    setTimeout(fn) { fn(); return 1; }, setInterval(fn) { for (let i = 0; i < 6; i++) fn(); return 1; }, clearInterval() {},
    fetch(url, options) { attempts.push({url, options}); throw new Error('offline sink: request not sent'); },
  }, ['TelemetryCollector']);
  const collector = new TelemetryCollector({getPerformanceStats: () => ({fps: 60, avgFrameTime: 16, jankPercent: 0, budgetUsed: 1}),
    getRendererType: () => 'webgpu', getNodeRegistry: () => ({nodes: new Map()})}, 'synthetic-test');
  return {collector, attempts, TELEMETRY_CONFIG};
}
test('actual collector start reaches POST sink; no external request is made', async () => {
  const {collector, attempts, TELEMETRY_CONFIG} = telemetryHarness();
  assert.equal(TELEMETRY_CONFIG.samplingRate, .05);
  await collector.start(); assert.equal(attempts.length, 1);
  assert.equal(attempts[0].url, 'https://shaders.com/api/telemetry');
  assert.equal(attempts[0].options.method, 'POST');
  assert.equal(JSON.parse(attempts[0].options.body).environment.domain, 'synthetic.invalid');
});
test('collector stopped before start does not sample or attempt POST', async () => {
  const {collector, attempts} = telemetryHarness(); collector.stop(); await collector.start(); assert.equal(attempts.length, 0);
});
test('pause before first render displays local still and does not initialize', async () => {
  const f = factory(), d = driver(f, 'early-pause');
  try {
    assert.equal(d.pause().reason, 'not_started');
    assert.equal((await d.drawFrame(5)).mode, 'static'); assert.equal(f.cores.length, 0);
    d.resume(); assert.equal((await d.drawFrame(5)).mode, 'dynamic');
  } finally { d.dispose(); }
  assert.equal(d.state().reason, 'disposed');
});
test('ambiguous node IDs and external mask references are rejected before starting', () => {
  const f = factory();
  for (const components of [[{type: 'Synthetic'}], [{id: 'x'}, {id: 'x'}], [{id: 'x', props: {maskSource: 'foreign'}}]]) {
    assert.throws(() => driver(f, 'bad-preset', {preset: {components}}), /node|mask/);
  }
  assert.equal(f.cores.length, 0);
});
