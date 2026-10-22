import { strict as assert } from 'node:assert';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { test } from 'node:test';

import { readCadRunEvidence, readCadRunArtifacts } from '../server/cad-run-evidence.ts';
import { watchCadArtifacts } from '../server/cad-artifact-stream.ts';

test('old failed drafts recover STEP as read-only unverified geometry and GLB as an attachment', async () => {
  const root = await mkdtemp(join(tmpdir(), 'a3d-legacy-draft-'));
  try {
    const runId = 'old-run';
    const directory = join(root, '.amagine3d-drafts', runId);
    await mkdir(directory, { recursive: true });
    const report = JSON.stringify({ schema: 'a3d-draft-result/v1', runId, status: 'failed', artifacts: {}, issues: [] });
    await writeFile(join(directory, 'draft-result.json'), report);
    await writeFile(join(directory, 'draft.step'), 'historical STEP');
    await writeFile(join(directory, 'draft.glb'), 'historical GLB');
    const items = await readCadRunArtifacts(root);
    assert.equal(items.length, 2);
    const step = items.find((item) => item.format === 'step');
    assert.equal(step?.status, 'diagnostic');
    assert.equal(step?.readOnly, true);
    assert.equal(step?.inputsCurrent, false);
    assert.equal(step?.manufacturingStatus, 'unknown');
    const attachment = items.find((item) => item.name === 'draft.glb');
    assert.equal(attachment?.kind, 'other');
    assert.equal(attachment?.format, undefined);
    assert.equal(await readFile(join(directory, 'draft-result.json'), 'utf8'), report);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('bound hidden draft is delivered before the turn ends and survives a newer failed run as stale evidence', async () => {
  const root = await mkdtemp(join(tmpdir(), 'a3d-live-draft-'));
  const bind = async (path: string) => ({ path, sha256: createHash('sha256').update(await readFile(path)).digest('hex') });
  let watcher: Awaited<ReturnType<typeof watchCadArtifacts>> | undefined;
  try {
    const folder = join(root, '.amagine3d-drafts', 'draft-run');
    await mkdir(folder, { recursive: true });
    await mkdir(join(root, '.a3d-agent'));
    const source = join(root, 'build.py');
    await writeFile(source, '# functional skeleton');
    const inputs = { source: await bind(source) };
    await writeFile(join(folder, 'display.step'), 'bound display bytes');
    await writeFile(join(folder, 'draft.step'), 'bound CAD bytes');
    await writeFile(join(folder, 'feature-cutter.stl'), 'internal cutter');
    const artifacts = { 'step:display': await bind(join(folder, 'display.step')), step: await bind(join(folder, 'draft.step')) };
    const geometryPath = join(folder, 'draft-geometry.json');
    await writeFile(geometryPath, JSON.stringify({ schema: 'a3d-draft-geometry/v2', runId: 'draft-run', artifacts }));
    const stagePath = join(folder, 'draft_cad-stage.json');
    await writeFile(stagePath, JSON.stringify({ schema: 'a3d-cad-stage/v2', runId: 'draft-run', units: 'mm',
      coordinateSystem: { handedness: 'right', up: 'Z' }, inputBindings: inputs,
      stages: { geometry: { status: 'passed' }, cadExport: { status: 'passed' } }, artifacts: { step: artifacts.step },
      audits: { step: { pass: true, sha256: artifacts.step.sha256 } } }));
    const reportPath = join(folder, 'draft-result.json');
    await writeFile(reportPath, JSON.stringify({ schema: 'a3d-draft-result/v2', runId: 'draft-run', status: 'draft', source: inputs.source,
      artifacts, geometry: await bind(geometryPath), cadStage: await bind(stagePath) }));
    const run = { report: await bind(reportPath), inputs };
    const statePath = join(root, '.a3d-agent', 'state.json');
    await writeFile(statePath, JSON.stringify({ schema: 'a3d-agent/v1' }));
    let active = true;
    const started = Date.now();
    let resolveUpdate: (value: number) => void = () => undefined;
    const update = new Promise<number>((resolve) => { resolveUpdate = resolve; });
    watcher = await watchCadArtifacts(root, async () => { resolveUpdate((await readCadRunArtifacts(root)).length); }, () => active);
    await writeFile(statePath, JSON.stringify({ schema: 'a3d-agent/v1', latest: run, runs: [run] }));
    assert.equal(await Promise.race([update, new Promise((_, reject) => setTimeout(() => reject(new Error('live draft timeout')), 1900))]), 2);
    assert.ok(Date.now() - started < 2000);
    const ready = await readCadRunArtifacts(root);
    assert.equal(ready.find((item) => item.format === 'step')?.purpose, 'draft-preview');
    assert.ok(ready.every((item) => item.inputsCurrent));
    assert.ok(ready.every((item) => item.designId === 'source:build.py'));
    assert.equal(ready.some((item) => item.name === 'feature-cutter.stl'), false);
    active = false;
    await watcher.stop();
    await writeFile(source, '# new revision');
    const failedPath = join(root, 'failed.json');
    await writeFile(failedPath, JSON.stringify({ schema: 'a3d-draft-result/v2', runId: 'failed-run', status: 'failed', artifacts: {} }));
    await writeFile(statePath, JSON.stringify({ schema: 'a3d-agent/v1', latest: { report: await bind(failedPath), inputs: { source: await bind(source) } }, runs: [run] }));
    const previous = await readCadRunArtifacts(root);
    assert.equal(previous.length, 2);
    assert.ok(previous.every((item) => item.inputsCurrent === false));
    assert.ok(previous.every((item) => item.designId === 'source:build.py'));
    await writeFile(join(folder, 'display.step'), 'tampered');
    const brokenDisplay = await readCadRunArtifacts(root);
    assert.equal(brokenDisplay.some((item) => item.purpose === 'draft-preview'), false);
    assert.equal(brokenDisplay.find((item) => item.purpose === 'cad-model')?.name, 'draft.step');
    // The same bound hidden report is available to legacy CLI sessions, with
    // stale input status retained; hidden feature files are never scanned.
    await rm(statePath);
    const legacy = await readCadRunArtifacts(root);
    assert.equal(legacy.length, 1);
    assert.equal(legacy[0]?.purpose, 'cad-model');
    assert.equal(legacy[0]?.inputsCurrent, false);
    assert.equal((await readCadRunEvidence(root))?.status, 'draft');
    active = true;
    const legacyUpdate = new Promise<number>((resolve) => { resolveUpdate = resolve; });
    watcher = await watchCadArtifacts(root, async () => { resolveUpdate((await readCadRunArtifacts(root)).length); }, () => active);
    watcher.refresh(true);
    assert.equal(await Promise.race([legacyUpdate, new Promise((_, reject) => setTimeout(() => reject(new Error('legacy refresh timeout')), 1900))]), 1);
    await watcher.stop();
    const stage = JSON.parse(await readFile(stagePath, 'utf8'));
    stage.audits.step.pass = false;
    await writeFile(stagePath, JSON.stringify(stage));
    const draft = JSON.parse(await readFile(reportPath, 'utf8'));
    draft.cadStage = await bind(stagePath);
    await writeFile(reportPath, JSON.stringify(draft));
    assert.deepEqual(await readCadRunArtifacts(root), []);
  } finally { await watcher?.stop(); await rm(root, { recursive: true, force: true }); }
});

test('bound latest CAD status remains independent of model outcome and input freshness', async () => {
  const root = await mkdtemp(join(tmpdir(), 'a3d-cad-evidence-'));
  const bound = async (path: string) => ({ path, sha256: createHash('sha256').update(await readFile(path)).digest('hex') });
  try {
    const source = join(root, 'build.py'), report = join(root, 'model_compile-result.json');
    await writeFile(source, '# source');
    const inputs = { source: await bound(source) };
    await writeFile(report, JSON.stringify({ schema: 'evidence-cad-compile-result/v2', runId: 'cad-run',
      status: 'awaiting-visual-review', pass: true, visualReviewRequired: true, inputBindings: inputs }));
    const reference = await bound(report);
    // Legacy and API state agree without claiming that an image was opened.
    const legacy = await readCadRunEvidence(root);
    assert.equal(legacy?.visualReviewRequired, true);
    await mkdir(join(root, '.a3d-agent'));
    await writeFile(join(root, '.a3d-agent', 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1', latest: { report: reference, inputs } }));
    assert.deepEqual(await readCadRunEvidence(root), legacy);
    const oldIntent = join(root, 'intent-v1.json'), newIntent = join(root, 'intent-v2.json');
    await writeFile(oldIntent, '{"revision":1}');
    await writeFile(newIntent, '{"revision":2}');
    const formalInputs = { ...inputs, intent: await bound(oldIntent) };
    await writeFile(join(root, '.a3d-agent', 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1',
      activeInput: { kind: 'source', path: source }, activeIntent: await bound(newIntent), latest: { report: reference, inputs: formalInputs } }));
    assert.equal((await readCadRunEvidence(root))?.inputsCurrent, false);
    await writeFile(join(root, '.a3d-agent', 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1',
      activeInput: { kind: 'source', path: source }, activeIntent: await bound(newIntent), latest: { report: reference, inputs } }));
    assert.equal((await readCadRunEvidence(root))?.inputsCurrent, true);
    const otherSource = join(root, 'other.py');
    await writeFile(otherSource, '# other design');
    await writeFile(join(root, '.a3d-agent', 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1',
      activeInput: { kind: 'source', path: otherSource }, latest: { report: reference, inputs } }));
    assert.equal((await readCadRunEvidence(root))?.inputsCurrent, false);
    assert.equal((await readCadRunEvidence(root))?.pass, true);
    await writeFile(source, '# changed');
    assert.equal((await readCadRunEvidence(root))?.inputsCurrent, false);
    await writeFile(report, '{}');
    assert.equal(await readCadRunEvidence(root), undefined);
    await symlink(join(root, '..'), join(root, 'outside'));
    await writeFile(join(root, '.a3d-agent', 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1', latest: { report: { path: '../file', sha256: 'a'.repeat(64) }, inputs } }));
    assert.equal(await readCadRunEvidence(root), undefined);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('every declared plate is retained as internal evidence on failed or stale runs', async () => {
  const root = await mkdtemp(join(tmpdir(), 'a3d-plate-evidence-'));
  const bind = async (path: string) => ({ path, sha256: createHash('sha256').update(await readFile(path)).digest('hex') });
  try {
    await mkdir(join(root, '.a3d-agent'), { recursive: true });
    const source = join(root, 'build.py');
    await writeFile(source, '# build');
    const inputs = { source: await bind(source) };
    const artifacts: Record<string, { path: string; sha256: string }> = {};
    for (const key of ['stl', '3mf', 'plate:02:stl', 'plate:02:3mf', 'internal-part.stl']) {
      const ext = key.endsWith('3mf') ? '3mf' : 'stl';
      const path = join(root, `${key.replaceAll(':', '-')}.${ext}`);
      await writeFile(path, `bytes for ${key}`);
      artifacts[key] = await bind(path);
    }
    const report = join(root, 'compile-result.json');
    const state = join(root, '.a3d-agent', 'state.json');
    const saveRun = async (status: string, pass: boolean) => {
      await writeFile(report, JSON.stringify({ schema: 'evidence-cad-compile-result/v2', runId: 'plate-run', status, pass, artifacts }));
      const run = { report: await bind(report), inputs };
      await writeFile(state, JSON.stringify({ schema: 'a3d-agent/v1', latest: run, runs: [run] }));
    };
    await saveRun('failed', false);
    const failed = await readCadRunArtifacts(root);
    assert.equal(failed.some((item) => item.path.endsWith('internal-part.stl')), false);
    assert.equal(failed.length, 4);
    assert.ok(failed.every((item) => item.purpose === 'internal-evidence' && item.status === 'failed'));

    await saveRun('awaiting-visual-review', true);
    await writeFile(source, '# changed source');
    const stale = await readCadRunArtifacts(root);
    assert.equal(stale.length, 4);
    assert.ok(stale.every((item) => item.inputsCurrent === false && item.purpose === 'internal-evidence' && !item.primary));
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('only explicitly registered hash-bound visual observations enter the artifact list', async () => {
  const root = await mkdtemp(join(tmpdir(), 'a3d-visual-preview-'));
  const bind = async (path: string) => ({ path, sha256: createHash('sha256').update(await readFile(path)).digest('hex') });
  try {
    const hidden = join(root, '.a3d-agent');
    const previewDir = join(hidden, 'previews', 'cache', 'cache-key');
    await mkdir(previewDir, { recursive: true });
    const source = join(root, 'model.py'), step = join(root, 'assembly.step'), objectStep = join(root, 'body.step');
    await writeFile(source, '# source');
    await writeFile(step, 'bound STEP');
    await writeFile(objectStep, 'bound object STEP');
    const stepRef = await bind(step);
    const objectRef = await bind(objectStep);
    const scenePath = `${step}.scene.json`;
    const scene = { schema: 'a3d-step-display/v1', step: stepRef,
      objects: [{ name: 'body', step: objectRef }] };
    const sceneBytes = JSON.stringify(scene);
    await writeFile(scenePath, sceneBytes);
    const sourceArtifact = { ...stepRef, scene: await bind(scenePath) };
    const inputs = { source: await bind(source) };
    const dependency = join(root, 'profile.json');
    await writeFile(dependency, '{"revision":1}');
    const exactRunInputs = { ...inputs, dependency: await bind(dependency) };
    const parentPath = join(hidden, 'runs', 'parent.json');
    await mkdir(join(hidden, 'runs'), { recursive: true });
    await writeFile(parentPath, JSON.stringify({ schema: 'a3d-draft-result/v2', runId: 'parent-run',
      status: 'draft', source: inputs.source, taskInputBindings: inputs, artifacts: {} }));
    const parentReport = await bind(parentPath);
    const image = join(previewDir, 'observation-00.png');
    const stray = join(previewDir, 'unregistered.png');
    await writeFile(image, 'bound image');
    await writeFile(stray, 'must stay hidden');
    const manifestPath = join(hidden, 'previews', 'cache-key.json');
    await writeFile(manifestPath, JSON.stringify({ schema: 'a3d-agent-preview/v1',
      parentReport, parentStatus: 'draft', sourceArtifact: { key: 'step:assembly', binding: sourceArtifact },
      observations: [{ id: 'front', kind: 'camera', image: await bind(image) }] }));
    const run = { report: parentReport, inputs: exactRunInputs };
    await writeFile(join(hidden, 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1',
      activeInput: { kind: 'source', path: source }, latest: run, runs: [run],
      visualPreviews: [{ manifest: await bind(manifestPath), parentReport }] }));

    const listed = await readCadRunArtifacts(root);
    assert.equal(listed.length, 1);
    assert.equal(listed[0]?.path, relative(root, image));
    assert.equal(listed[0]?.kind, 'image');
    assert.equal(listed[0]?.purpose, 'observation-preview');
    assert.equal(listed[0]?.inputsCurrent, true);
    assert.equal(listed.some((item) => item.path.endsWith('unregistered.png')), false);

    const newerReport = join(hidden, 'runs', 'newer.json');
    await writeFile(newerReport, JSON.stringify({ schema: 'a3d-draft-result/v2', runId: 'newer-run',
      status: 'draft', source: inputs.source, taskInputBindings: inputs, artifacts: {} }));
    await writeFile(join(hidden, 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1',
      activeInput: { kind: 'source', path: source }, latest: { report: await bind(newerReport), inputs }, runs: [run],
      visualPreviews: [{ manifest: await bind(manifestPath), parentReport }] }));
    const historical = await readCadRunArtifacts(root);
    assert.equal(historical.length, 1);
    assert.equal(historical[0]?.inputsCurrent, false);

    await writeFile(join(hidden, 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1',
      activeInput: { kind: 'source', path: source }, latest: run, runs: [run],
      visualPreviews: [{ manifest: await bind(manifestPath), parentReport }] }));
    await writeFile(dependency, '{"revision":2}');
    const staleDependency = await readCadRunArtifacts(root);
    assert.equal(staleDependency.length, 1);
    assert.equal(staleDependency[0]?.inputsCurrent, false);
    await writeFile(dependency, '{"revision":1}');
    const restoredInputs = { ...inputs, dependency: await bind(dependency) };
    const restoredRun = { report: parentReport, inputs: restoredInputs };
    await writeFile(join(hidden, 'state.json'), JSON.stringify({ schema: 'a3d-agent/v1',
      activeInput: { kind: 'source', path: source }, latest: restoredRun, runs: [restoredRun],
      visualPreviews: [{ manifest: await bind(manifestPath), parentReport }] }));

    const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
    manifest.sourceArtifact.binding = sourceArtifact;
    await writeFile(manifestPath, JSON.stringify(manifest));
    const statePath = join(hidden, 'state.json');
    await writeFile(statePath, JSON.stringify({ schema: 'a3d-agent/v1', activeInput: { kind: 'source', path: source },
      latest: run, runs: [run], visualPreviews: [{ manifest: await bind(manifestPath), parentReport }] }));
    const rebound = await readCadRunArtifacts(root);
    assert.equal(rebound.length, 1, JSON.stringify(rebound));
    await writeFile(scenePath, sceneBytes + 'tampered');
    assert.deepEqual(await readCadRunArtifacts(root), []);
    await writeFile(scenePath, sceneBytes);
    await writeFile(objectStep, 'tampered object STEP');
    assert.deepEqual(await readCadRunArtifacts(root), []);
    await writeFile(objectStep, 'bound object STEP');
    const currentManifestRef = await bind(manifestPath);
    await writeFile(statePath, JSON.stringify({ schema: 'a3d-agent/v1', activeInput: { kind: 'source', path: source },
      latest: run, runs: [run], visualPreviews: [{ manifest: currentManifestRef, parentReport }] }));
    await writeFile(image, 'tampered');
    assert.deepEqual(await readCadRunArtifacts(root), []);
  } finally { await rm(root, { recursive: true, force: true }); }
});
