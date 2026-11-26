// Local native-editor adapter; the Python music pipeline owns all policy.
import {readFileSync, writeFileSync, mkdirSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {dirname, resolve, delimiter} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export function assertNativeMusicArrangement(plan, {reportPath, python = 'python'}) {
  const fps = plan.fps;
  if (!Number.isFinite(fps) || fps <= 0) throw Error('BGM preflight requires explicit fps');
  const rows = plan.choices ?? plan.clips;
  if (!Array.isArray(rows)) throw Error('BGM preflight requires the complete clip list');
  const asset_sources = {};
  const layers = rows.map((c, index) => {
    const source = c.source ?? plan.source;
    if (!source?.path || !source?.sha256) throw Error('BGM preflight requires hash-bound sources');
    const asset_id = c.asset ?? `source-${source.sha256}`;
    const asset = {path: source.path, sha256: source.sha256,
      ...(source.duration_s === undefined ? {} : {duration_s: source.duration_s})};
    if (asset_sources[asset_id] && JSON.stringify(asset_sources[asset_id]) !== JSON.stringify(asset)) {
      throw Error('Conflicting BGM asset metadata');
    }
    asset_sources[asset_id] = asset;
    return {asset_id, segment_id: String(index), timeline_start_s: c.start / fps,
      duration_s: c.duration / fps, source_start_s: (c.sourceStartFrame ?? 0) / fps,
      ...(c.sourceEndFrame === undefined ? {} : {source_end_s: c.sourceEndFrame / fps}),
      loop: c.loop ?? false};
  });
  const output = resolve(reportPath), spec = output + '.input.json';
  mkdirSync(dirname(output), {recursive: true});
  writeFileSync(spec, JSON.stringify({layers, asset_sources}, null, 2));
  const result = spawnSync(python, ['-X', 'utf8', '-m', 'video_creation_pipeline.music_cli',
    'audit-arrangement', '--spec', spec, '--output', output], {
    // Inherit handles: sandboxed Windows hosts may deny Node's private pipes.
    cwd: root, stdio: 'inherit', windowsHide: true, timeout: 60000,
    env: {...process.env, PYTHONPATH: [resolve(root, 'src'), process.env.PYTHONPATH].filter(Boolean).join(delimiter)},
  });
  if (result.error || result.status !== 0) {
    const error = Error(`BGM arrangement blocked before editor writes; use longer or varied sources. ${result.error ?? ''} Report: ${output}`);
    error.auditExitCode = result.status;
    throw error;
  }
  return JSON.parse(readFileSync(output, 'utf8'));
}
