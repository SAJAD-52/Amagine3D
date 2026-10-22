import { createHash } from 'node:crypto';
import { readFile, readdir, realpath, stat } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';

import type { ArtifactSummary, CadRunEvidence } from '../src/types.ts';

type BoundFile = { path: string; sha256: string };
interface RunRecord { report: BoundFile; inputs?: Record<string, BoundFile>; design?: { revision?: string } }
interface ActiveInput { kind?: string; path?: string; intentPath?: string }

async function runRecords(root: string): Promise<{ state: Record<string, any>; records: RunRecord[] }> {
  let state: Record<string, any> = {};
  try {
    const path = await realpath(join(root, '.a3d-agent/state.json'));
    if (!path.startsWith(root + sep) || (await stat(path)).size > 2 * 1024 * 1024) throw new Error('invalid state');
    state = JSON.parse(await readFile(path, 'utf8'));
    if (state.schema !== 'a3d-agent/v1') throw new Error('invalid state');
  } catch (error) {
    if (!error || typeof error !== 'object' || !('code' in error) || error.code !== 'ENOENT') throw error;
  }
  const records = [state.latest, ...(Array.isArray(state.runs) ? state.runs.slice(-10).reverse() : [])].filter(Boolean);
  if (records.length) return { state, records };
  // Known legacy report locations only. This does not expose hidden folders to
  // normal scanning or follow another workspace's reports.
  const candidates = (await readdir(root)).filter((name) => name.endsWith('_compile-result.json')).map((name) => join(root, name));
  try {
    for (const entry of await readdir(join(root, '.amagine3d-drafts'), { withFileTypes: true })) {
      if (entry.isDirectory()) candidates.push(join(root, '.amagine3d-drafts', entry.name, 'draft-result.json'));
    }
  } catch (error) {
    if (!error || typeof error !== 'object' || !('code' in error) || error.code !== 'ENOENT') throw error;
  }
  const metadata = await Promise.allSettled(candidates.map(async (path) => {
    const canonical = await realpath(path);
    if (!canonical.startsWith(root + sep)) throw new Error('out of scope');
    const info = await stat(canonical);
    if (info.size > 2 * 1024 * 1024) throw new Error('oversized report');
    return { path: canonical, modified: info.mtimeMs };
  }));
  const recent = metadata.flatMap((entry) => entry.status === 'fulfilled' ? [entry.value] : []).sort((a, b) => b.modified - a.modified).slice(0, 10);
  for (const candidate of recent) {
    try {
      const bytes = await readFile(candidate.path);
      const doc = JSON.parse(bytes.toString('utf8'));
      if (!['a3d-draft-result/v2', 'evidence-cad-compile-result/v2', 'a3d-draft-result/v1', 'evidence-cad-compile-result/v1'].includes(doc.schema) || !doc.runId) continue;
      records.push({ report: { path: candidate.path, sha256: createHash('sha256').update(bytes).digest('hex') },
        inputs: doc.taskInputBindings ?? doc.inputBindings ?? (doc.source ? { source: doc.source } : {}) });
    } catch { /* One malformed historical report cannot hide another valid one. */ }
  }
  return { state, records };
}

async function activeInput(root: string, state: Record<string, any>): Promise<ActiveInput | undefined> {
  const intent = typeof state.activeIntent?.path === 'string' ? { intentPath: resolve(root, state.activeIntent.path) } : {};
  if (['design', 'source'].includes(state.activeInput?.kind) && typeof state.activeInput.path === 'string') {
    return { kind: state.activeInput.kind, path: resolve(root, state.activeInput.path), ...intent };
  }
  try {
    const path = await realpath(join(root, '.a3d-agent/design.json'));
    if (!path.startsWith(root + sep)) return undefined;
    const pointer = JSON.parse(await readFile(path, 'utf8'));
    if (typeof pointer.path === 'string') return { kind: 'design', path: resolve(root, pointer.path), ...intent };
  } catch { /* Python authoring has no design pointer. */ }
  return intent.intentPath ? intent : undefined;
}

/** Resolve the explicitly active design pointer and its hash, plus a hash-bound intent if present. */
export async function readActiveDesignBinding(workspace: string): Promise<{
  designPath: string; revision: string; intentPath?: string; intentStale?: boolean;
  buildStatus?: 'draft' | 'failed' | 'validated';
} | undefined> {
  try {
    const root = await realpath(workspace);
    const { state, records } = await runRecords(root);
    const active = await activeInput(root, state);
    if (active?.kind !== 'design' || !active.path) return undefined;
    const designPath = await realpath(active.path);
    const local = relative(root, designPath);
    if (!local || isAbsolute(local) || local === '..' || local.startsWith(`..${sep}`)) return undefined;
    const bytes = await readFile(designPath);
    const revision = createHash('sha256').update(bytes).digest('hex');
    let intentPath: string | undefined;
    const activeIntent = state.activeIntent;
    let activeIntentValid = false;
    if (activeIntent?.path && activeIntent?.sha256) {
      try {
        const path = await realpath(resolve(root, activeIntent.path));
        const intentLocal = relative(root, path);
        if (intentLocal && !isAbsolute(intentLocal) && intentLocal !== '..' && !intentLocal.startsWith(`..${sep}`) &&
            createHash('sha256').update(await readFile(path)).digest('hex') === activeIntent.sha256) {
          intentPath = intentLocal.split(sep).join('/');
          activeIntentValid = true;
        }
      } catch { /* A stale active intent is not a usable build input. */ }
    }
    let buildStatus: 'draft' | 'failed' | 'validated' | undefined;
    for (const record of records) {
      if (!record.report) continue;
      try {
        const reportPath = await realpath(resolve(root, record.report.path));
        const reportLocal = relative(root, reportPath);
        if (!reportLocal || isAbsolute(reportLocal) || reportLocal === '..' || reportLocal.startsWith(`..${sep}`) || (await stat(reportPath)).size > 2 * 1024 * 1024) continue;
        const reportBytes = await readFile(reportPath);
        if (createHash('sha256').update(reportBytes).digest('hex') !== record.report.sha256) continue;
        const doc = JSON.parse(reportBytes.toString('utf8'));
        if (!['evidence-cad-compile-result/v2', 'a3d-draft-result/v2'].includes(doc.schema) || typeof doc.runId !== 'string') continue;
        const inputs = record.inputs ?? doc.taskInputBindings ?? doc.inputBindings ?? {};
        const design = inputs.design;
        if (!design || resolve(root, design.path) !== designPath || design.sha256 !== revision) continue;
        buildStatus = doc.pass === true ? 'validated' : doc.status === 'draft' ? 'draft' : 'failed';
        const intent = inputs.intent;
        if (!activeIntent?.path && !intentPath && intent?.path && intent?.sha256) {
          const path = await realpath(resolve(root, intent.path));
          const intentLocal = relative(root, path);
          if (intentLocal && !isAbsolute(intentLocal) && intentLocal !== '..' && !intentLocal.startsWith(`..${sep}`) &&
              createHash('sha256').update(await readFile(path)).digest('hex') === intent.sha256) intentPath = intentLocal.split(sep).join('/');
        }
        break;
      } catch { /* A malformed historical report cannot hide the current design. */ }
    }
    return { designPath: local.split(sep).join('/'), revision, ...(intentPath ? { intentPath } : {}),
      ...(activeIntent?.path && !activeIntentValid ? { intentStale: true } : {}), ...(buildStatus ? { buildStatus } : {}) };
  } catch { return undefined; }
}

function matchesActive(root: string, active: ActiveInput | undefined, inputs: Record<string, BoundFile>): boolean {
  return (!active?.kind || Boolean(inputs[active.kind] && resolve(root, inputs[active.kind]!.path) === active.path)) &&
    (!active?.intentPath || !inputs.intent || resolve(root, inputs.intent.path) === active.intentPath);
}

/** Read only the API's bound latest result; never infer success from file presence. */
export async function readCadRunEvidence(workspace: string): Promise<CadRunEvidence | undefined> {
  try {
    const root = await realpath(workspace);
    const scopedPath = async (value: string) => {
      const path = await realpath(resolve(root, value));
      const fromRoot = relative(root, path);
      if (isAbsolute(fromRoot) || fromRoot === '..' || fromRoot.startsWith(`..${sep}`)) throw new Error('out of scope');
      return path;
    };
    const readBound = async (value: { path: string; sha256: string }) => {
      const path = await scopedPath(value.path);
      const bytes = await readFile(path);
      if (createHash('sha256').update(bytes).digest('hex') !== value.sha256) throw new Error('stale evidence');
      return bytes;
    };
    const { state, records } = await runRecords(root);
    const latest = records[0];
    if (!latest) return undefined;
    const report = { ...latest.report, path: await scopedPath(latest.report.path) };
    const document = JSON.parse((await readBound(report)).toString('utf8'));
    if (!['evidence-cad-compile-result/v2', 'a3d-draft-result/v2'].includes(document.schema) ||
        typeof document.runId !== 'string' || typeof document.status !== 'string') return undefined;
    const inputs = latest.inputs ?? document.taskInputBindings ?? document.inputBindings ?? {};
    const matches = await Promise.allSettled(Object.values(inputs).map((ref) => readBound(ref as BoundFile)));
    return { runId: document.runId, report, status: document.status,
      pass: document.pass === true, visualReviewRequired: document.visualReviewRequired !== false,
      inputsCurrent: matches.length > 0 && matches.every((entry) => entry.status === 'fulfilled') && matchesActive(root, await activeInput(root, state), inputs),
      ...(latest.design?.revision ? { revision: latest.design.revision } : {}),
      ...(document.artifactStages ? { stages: document.artifactStages } : {}) };
  } catch {
    return undefined;
  }
}

/** Cheap change detector for a running turn; output files are read only on change. */
export async function cadArtifactVersion(workspace: string): Promise<string | undefined> {
  try {
    const root = await realpath(workspace);
    const statePath = await realpath(join(root, '.a3d-agent/state.json'));
    if (!statePath.startsWith(root + sep) || (await stat(statePath)).size > 2 * 1024 * 1024) return undefined;
    const bytes = await readFile(statePath);
    return createHash('sha256').update(bytes).digest('hex');
  } catch { return undefined; }
}

/** Explicit bound run records are the only gateway into hidden CAD output folders. */
export async function readCadRunArtifacts(workspace: string): Promise<ArtifactSummary[]> {
  try {
    const root = await realpath(workspace);
    const scoped = async (value: string) => {
      const path = await realpath(resolve(root, value));
      const local = relative(root, path);
      if (!local || isAbsolute(local) || local === '..' || local.startsWith(`..${sep}`)) throw new Error('out of scope');
      return path;
    };
    const bound = async (ref: { path: string; sha256: string }, maxBytes = 128 * 1024 * 1024) => {
      if (!ref || typeof ref.path !== 'string' || !/^[a-f0-9]{64}$/u.test(ref.sha256)) throw new Error('invalid binding');
      const path = await scoped(ref.path);
      if ((await stat(path)).size > maxBytes) throw new Error('oversized evidence');
      const bytes = await readFile(path);
      if (createHash('sha256').update(bytes).digest('hex') !== ref.sha256) throw new Error('stale evidence');
      return { path, bytes };
    };
    const { state, records } = await runRecords(root);
    const active = await activeInput(root, state);
    const seenReports = new Set<string>();
    const artifacts = new Map<string, ArtifactSummary>();
    for (const run of records) {
      try {
        if (!run?.report || seenReports.has(run.report.sha256)) continue;
        seenReports.add(run.report.sha256);
        const { bytes, path: runReportPath } = await bound(run.report, 2 * 1024 * 1024);
        const doc = JSON.parse(bytes.toString('utf8'));
        if (!['a3d-draft-result/v2', 'evidence-cad-compile-result/v2', 'a3d-draft-result/v1', 'evidence-cad-compile-result/v1'].includes(doc.schema) ||
          typeof doc.runId !== 'string' || !doc.runId) continue;
        const inputs = run.inputs ?? doc.taskInputBindings ?? doc.inputBindings ?? (doc.source ? { source: doc.source } : {});
        const designInput = inputs.design ?? inputs.source;
        const designPath = typeof designInput?.path === 'string'
          ? relative(root, await realpath(resolve(root, designInput.path)).catch(() => resolve(root, designInput.path))) : undefined;
        const designId = designPath && !isAbsolute(designPath) && designPath !== '..' && !designPath.startsWith(`..${sep}`)
          ? `${inputs.design ? 'design' : 'source'}:${designPath.split(sep).join('/')}` : undefined;
        const freshness = await Promise.allSettled(Object.values(inputs).map((ref) => bound(ref as { path: string; sha256: string })));
        const current = freshness.length > 0 && freshness.every((item) => item.status === 'fulfilled') &&
          matchesActive(root, active, inputs);
        const intentPath = inputs.intent?.path ? relative(root, resolve(root, inputs.intent.path)).split(sep).join('/') : undefined;
        const revision = run.design?.revision ?? inputs.design?.sha256 ?? inputs.source?.sha256;
        const status = doc.pass === true ? 'validated' as const : doc.status === 'draft' ? 'draft' as const : 'failed' as const;
        // Old display failures wrote STEP before they could write artifact
        // bindings. Recover only those known draft files as unverified snapshots.
        // Never upgrade the old report or infer manufacturing acceptance.
        if (doc.schema === 'a3d-draft-result/v1' && !doc.cadStage && basename(dirname(runReportPath)) === doc.runId &&
            dirname(dirname(runReportPath)) === join(root, '.amagine3d-drafts')) {
          for (const extension of ['step', 'glb']) {
            try {
              const path = await scoped(join(dirname(runReportPath), 'draft.' + extension));
              const ref = doc.artifacts?.[extension];
              const bytes = ref ? (await bound(ref)).bytes : await readFile(path);
              if (bytes.length > 128 * 1024 * 1024) continue;
              const info = await stat(path);
              const local = relative(root, path).split(sep).join('/');
              if (artifacts.has(local)) continue;
              artifacts.set(local, { legacy: true, readOnly: true, kind: extension === 'step' ? 'model' : 'other',
                ...(extension === 'step' ? { format: 'step' as const } : {}),
                name: 'draft.' + extension, path: local, url: `/api/artifacts/file?path=${encodeURIComponent(local)}`,
                size: info.size, modifiedAt: info.mtime.toISOString(), sha256: createHash('sha256').update(bytes).digest('hex'),
                primary: extension === 'step', purpose: extension === 'step' ? 'cad-model' : 'unclassified',
                status: 'diagnostic', inputsCurrent: false, buildId: doc.runId, manufacturingStatus: 'unknown',
                ...(designId ? { designId } : {}) });
            } catch { /* Missing legacy STEP/attachment stays unavailable. */ }
          }
        }
        let declared = doc.artifacts ?? {};
        let modelId: string | undefined;
        if (doc.artifacts?.buildReport) {
          const report = JSON.parse((await bound(doc.artifacts.buildReport, 2 * 1024 * 1024)).bytes.toString('utf8'));
          if (['evidence-a3d-build/v2', 'evidence-a3d-build/v1'].includes(report.schema)) {
            declared = { ...report.artifacts, ...declared };
            if (typeof report.part === 'string' && report.part.trim()) modelId = report.part;
          }
        }
        if (doc.geometry) {
          const manifest = JSON.parse((await bound(doc.geometry, 2 * 1024 * 1024)).bytes.toString('utf8'));
          if (!['a3d-draft-geometry/v2', 'a3d-draft-geometry/v1'].includes(manifest.schema) || manifest.runId !== doc.runId) continue;
          if (Object.keys(manifest.artifacts ?? {}).length !== Object.keys(declared).length ||
              Object.entries(declared).some(([key, value]) => JSON.stringify(manifest.artifacts?.[key]) !== JSON.stringify(value))) continue;
        }
        let cadVerified = false;
        if (doc.cadStage) {
          const stage = JSON.parse((await bound(doc.cadStage, 2 * 1024 * 1024)).bytes.toString('utf8'));
          if (!['a3d-cad-stage/v2', 'a3d-cad-stage/v1'].includes(stage.schema) || stage.runId !== doc.runId || stage.units !== 'mm' ||
              stage.coordinateSystem?.handedness !== 'right' || stage.coordinateSystem?.up !== 'Z' ||
              !stage.inputBindings?.source || Object.entries(stage.inputBindings).some(([key, raw]) => {
                const ref = raw as BoundFile;
                return !inputs[key] || inputs[key].sha256 !== ref.sha256 || resolve(root, inputs[key].path) !== resolve(root, ref.path);
              })) continue;
          cadVerified = stage.stages?.geometry?.status === 'passed' && stage.stages?.cadExport?.status === 'passed' &&
            Object.keys(stage.artifacts ?? {}).length > 0 && Object.entries(stage.artifacts).every(([key, raw]) =>
              stage.audits?.[key]?.pass === true && stage.audits[key].sha256 === (raw as BoundFile).sha256);
          if (!cadVerified) continue;
          declared = { ...declared, ...stage.artifacts };
        }
        const cadMasterKey = declared['step:assembly'] ? 'step:assembly' : declared.step ? 'step'
          : Object.keys(declared).find((key) => key.startsWith('step:') && key !== 'step:display');
        let previewImagePath: string | undefined;
        const imageRef = doc.artifacts?.preview ?? doc.artifacts?.diagnosticPreview;
        if (imageRef) {
          try { const image = await bound(imageRef); if (image.path.endsWith('.png')) previewImagePath = relative(root, image.path).split(sep).join('/'); } catch { /* Preview failure retains CAD. */ }
        }
        for (const [key, raw] of Object.entries(declared)) {
          try {
          const legacyAttachment = doc.schema.endsWith('/v1') && ['glb', 'glb:display'].includes(key);
          const platePrint = /^plate:\d+:(?:stl|3mf)$/u.test(key);
          if (!legacyAttachment && !['step:display', 'step', 'stl', '3mf', 'preview', 'diagnosticPreview'].includes(key) && !key.startsWith('step:') && !platePrint) continue;
          const ref = raw as { path: string; sha256: string };
          const { path } = await bound(ref);
          const local = relative(root, path).split(sep).join('/');
          // Run evidence cannot promote arbitrary internal STL/3MF to print packages.
          const display = key === 'step:display' && /\.st(?:ep|p)$/iu.test(path);
          const step = cadVerified && (key === 'step' || key.startsWith('step:')) && /\.st(?:ep|p)$/iu.test(path);
          const print = (key === 'stl' || key === '3mf' || platePrint) && /\.(?:stl|3mf)$/iu.test(path);
          const image = ['preview', 'diagnosticPreview'].includes(key) && /\.png$/iu.test(path);
          if (!display && !step && !print && !image && !legacyAttachment) continue;
          if (artifacts.has(local)) continue;
          const meta = await stat(path);
          artifacts.set(local, { kind: legacyAttachment ? 'other' : image ? 'image' : 'model', name: local.split('/').at(-1)!, path: local,
            url: `/api/artifacts/file?path=${encodeURIComponent(local)}`, size: meta.size,
            modifiedAt: meta.mtime.toISOString(), primary: display || (step ? key === cadMasterKey : image ? ['preview', 'diagnosticPreview'].includes(key) : status === 'validated' && current), buildId: doc.runId,
            ...(designId ? { designId } : {}), ...(designPath && inputs.design ? { designPath } : {}),
            ...(modelId ? { modelId } : {}),
            ...(step && !display && key !== cadMasterKey ? { partId: key.slice('step:'.length) } : {}),
            ...(platePrint ? { plateId: key.split(':')[1] } : {}),
            ...(doc.artifactStages?.manufacturing?.status ? { manufacturingStatus: doc.artifactStages.manufacturing.status } : {}),
            ...(doc.schema.endsWith('/v1') ? { legacy: true, readOnly: true } : {}),
            ...(revision ? { revision } : {}), inputsCurrent: current, status, sha256: ref.sha256,
            ...(previewImagePath && (display || step) ? { previewImagePath } : {}),
            purpose: display ? status === 'validated' ? 'display-preview' : status === 'draft' ? 'draft-preview' : 'diagnostic-preview' : step ? 'cad-model' : print ? status === 'validated' && current ? 'print-package' : 'internal-evidence'
              : status === 'validated' ? 'display-preview' : status === 'draft' ? 'draft-preview' : 'diagnostic-preview',
            ...(display || step ? { format: 'step' as const } : print ? { format: path.endsWith('.3mf') ? '3mf' as const : 'stl' as const } : {}),
          });
          } catch { /* A broken display file cannot discard an independently bound CAD export. */ }
        }
      } catch { /* A missing/stale historical run cannot replace valid current evidence. */ }
    }
    // On-demand observations live under the private cache directory, so they
    // become visible only through a bounded registry of exact report/image
    // bindings written by the agent API. Never walk .a3d-agent recursively.
    const previewEntries = Array.isArray(state.visualPreviews) ? state.visualPreviews.slice(-32) : [];
    for (const entry of previewEntries) {
      try {
        if (!entry?.manifest || !entry?.parentReport) continue;
        const { bytes: manifestBytes } = await bound(entry.manifest, 8 * 1024 * 1024);
        const manifest = JSON.parse(manifestBytes.toString('utf8'));
        if (manifest.schema !== 'a3d-agent-preview/v1' || manifest.parentReport.path !== entry.parentReport.path ||
            manifest.parentReport.sha256 !== entry.parentReport.sha256 || !Array.isArray(manifest.observations)) continue;
        const { bytes: parentBytes } = await bound(entry.parentReport, 2 * 1024 * 1024);
        const parent = JSON.parse(parentBytes.toString('utf8'));
        if (!['a3d-draft-result/v2', 'evidence-cad-compile-result/v2'].includes(parent.schema) || !parent.runId) continue;
        const sourceRef = manifest.sourceArtifact?.binding;
        if (!sourceRef) continue;
        const { path: sourcePath } = await bound(sourceRef);
        const expectedScene = `${sourcePath}.scene.json`;
        let sceneOnDisk = false;
        try { await stat(expectedScene); sceneOnDisk = true; } catch { /* Plain STEP files may have no descriptor. */ }
        if (sceneOnDisk && !sourceRef.scene) continue;
        if (sourceRef.scene) {
          const { bytes: sceneBytes, path: scenePath } = await bound(sourceRef.scene, 8 * 1024 * 1024);
          if (scenePath !== expectedScene) continue;
          const scene = JSON.parse(sceneBytes.toString('utf8'));
          if (scene.schema !== 'a3d-step-display/v1' ||
              scene.step?.path !== sourceRef.path || scene.step?.sha256 !== sourceRef.sha256 ||
              !Array.isArray(scene.objects) || !scene.objects.length || scene.objects.length > 2_000) continue;
          const objectNames = new Set<string>();
          let sceneCurrent = true;
          for (const object of scene.objects) {
            if (!object || typeof object.name !== 'string' || !object.name.trim() || objectNames.has(object.name)) {
              sceneCurrent = false; break;
            }
            objectNames.add(object.name);
            try { await bound(object.step); }
            catch { sceneCurrent = false; break; }
          }
          if (!sceneCurrent || !scenePath.startsWith(root + sep) || !sourcePath.startsWith(root + sep)) continue;
        }
        const parentRun = [state.latest, ...(Array.isArray(state.runs) ? state.runs : [])].find((run: any) =>
          run?.report?.path === entry.parentReport.path && run?.report?.sha256 === entry.parentReport.sha256);
        const inputs = parentRun?.inputs ?? parent.taskInputBindings ?? parent.inputBindings ??
          (parent.source ? { source: parent.source } : {});
        const freshness = await Promise.allSettled(Object.values(inputs).map((ref) => bound(ref as BoundFile)));
        const latestReport = state.latest?.report;
        const latestRunMatches = latestReport?.path === entry.parentReport.path &&
          latestReport?.sha256 === entry.parentReport.sha256;
        const current = freshness.length > 0 && freshness.every((item) => item.status === 'fulfilled') &&
          matchesActive(root, active, inputs) && latestRunMatches;
        const designInput = inputs.design ?? inputs.source;
        const designPath = typeof designInput?.path === 'string'
          ? relative(root, await realpath(resolve(root, designInput.path)).catch(() => resolve(root, designInput.path))) : undefined;
        const designId = designPath && !isAbsolute(designPath) && designPath !== '..' && !designPath.startsWith(`..${sep}`)
          ? `${inputs.design ? 'design' : 'source'}:${designPath.split(sep).join('/')}` : undefined;
        const status = parent.pass === true ? 'validated' as const : parent.status === 'draft' ? 'draft' as const : 'diagnostic' as const;
        for (const observation of manifest.observations) {
          try {
            const imageRef = observation?.image;
            if (!imageRef || typeof imageRef.path !== 'string' || !imageRef.path.toLowerCase().endsWith('.png')) continue;
            const { path, bytes } = await bound(imageRef, 32 * 1024 * 1024);
            const local = relative(root, path).split(sep).join('/');
            if (artifacts.has(local)) continue;
            const info = await stat(path);
            artifacts.set(local, { kind: 'image', name: basename(path), path: local,
              url: `/api/artifacts/file?path=${encodeURIComponent(local)}`, size: info.size,
              modifiedAt: info.mtime.toISOString(), primary: false, purpose: 'observation-preview',
              status, inputsCurrent: current, sha256: createHash('sha256').update(bytes).digest('hex'),
              buildId: parent.runId, ...(designId ? { designId } : {}), ...(parent.model ? { modelId: parent.model } : {}) });
          } catch { /* One damaged image does not hide other bound observations. */ }
        }
      } catch { /* Stale manifests, parent reports, or STEP sources stay hidden. */ }
    }
    return [...artifacts.values()];
  } catch { return []; }
}
