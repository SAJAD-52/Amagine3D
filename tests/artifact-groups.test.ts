import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { artifactDesigns, selectedDesignVersion } from '../src/lib/artifact-groups.ts';
import { latestArtifactPreview } from '../src/lib/artifact-updates.ts';
import { defaultPreviewArtifact, displayPreviewForSelection, printPreviewForSelection } from '../src/lib/artifact-selection.ts';
import type { ArtifactSummary } from '../src/types.ts';

function file(buildId: string, path: string, patch: Partial<ArtifactSummary> = {}): ArtifactSummary {
  return { buildId, designId: 'design:robot.a3d.json', modelId: 'robot', kind: 'model', format: 'step',
    primary: true, purpose: 'display-preview', status: 'validated', inputsCurrent: true,
    modifiedAt: '2026-10-15T10:00:00Z', name: path.split('/').at(-1)!, path, size: 1, url: `/files/${path}`, ...patch };
}

test('one complete design owns twenty parts, their formats, three plates and multiple builds', () => {
  const old = file('old', 'old/display.step', { modifiedAt: '2026-10-15T09:00:00Z', inputsCurrent: false });
  const current = file('current', 'current/display.step');
  const parts = Array.from({ length: 20 }, (_, index) => ['step', 'stl'].map((format) => file('current', `current/part-${index}.${format}`, {
    partId: `part-${index}`, primary: false, purpose: 'unclassified', format: format as 'step' | 'stl',
  }))).flat();
  const plates = ['01', '02', '03'].flatMap((plateId) => ['3mf', 'stl'].map((format) => file('current', `current/plate-${plateId}.${format}`, {
    plateId, purpose: 'print-package', format: format as '3mf' | 'stl',
  })));
  const designs = artifactDesigns([old, current, ...parts, ...plates]);
  assert.equal(designs.length, 1);
  assert.equal(designs[0]!.versions.length, 2);
  assert.equal(designs[0]!.latest.preview, current);
  assert.equal(designs[0]!.latest.parts.length, 20);
  assert.ok(designs[0]!.latest.parts.every((part) => part.artifacts.length === 2));
  assert.equal(designs[0]!.latest.plates.length, 3);
  assert.equal(defaultPreviewArtifact([...parts, current]), current);
  assert.equal(selectedDesignVersion(designs[0]!, old.path).id, 'old');
  assert.equal(selectedDesignVersion(designs[0]!, parts[0]!.path).id, 'current');
});

test('new drafts appear immediately alongside the previous validated version and survive failed attempts', () => {
  const result = file('validated', 'validated/display.step', { modifiedAt: '2026-10-15T08:00:00Z', inputsCurrent: false });
  const draft = file('draft', '.amagine3d-drafts/draft/display.step', { purpose: 'draft-preview', status: 'draft', inputsCurrent: false });
  const failed = file('failed', 'failed/display.step', { modifiedAt: '2026-10-15T11:00:00Z', purpose: 'diagnostic-preview', status: 'failed' });
  const [design] = artifactDesigns([result, draft, failed]);
  assert.equal(design?.latest.id, 'draft');
  assert.equal(design?.draft?.preview, draft);
  assert.equal(design?.validated?.preview, result);
  assert.equal(latestArtifactPreview([result, draft, failed]), draft);
  assert.equal(design?.latest.prints.length, 0);

  const final = file('final', 'final/display.step', { modifiedAt: '2026-10-15T12:00:00Z' });
  const [completed] = artifactDesigns([result, draft, failed, final]);
  assert.equal(completed?.latest.preview, final);
  assert.equal(completed?.draft, undefined);
  assert.equal(completed?.versions.length, 4);
});

test('independent design identities remain separate even with matching display filenames', () => {
  const first = file('first', 'one/draft.step', { modelId: undefined, status: 'draft', purpose: 'draft-preview' });
  const second = file('second', 'two/draft.step', { modelId: undefined, designId: 'design:dock.a3d.json', status: 'draft', purpose: 'draft-preview' });
  assert.deepEqual(new Set(artifactDesigns([first, second]).map((design) => design.name)), new Set(['robot', 'dock']));
});

test('late observation images stay in their build files without changing version recency or default preview', () => {
  const old = file('old', 'old/display.step', { modifiedAt: '2026-10-15T09:00:00Z' });
  const current = file('current', 'current/display.step', { modifiedAt: '2026-10-15T10:00:00Z' });
  const observation = { ...file('old', 'old/observation.png', { kind: 'image', format: undefined,
    purpose: 'observation-preview', primary: false, modifiedAt: '2026-10-15T11:00:00Z' }),
  kind: 'image' as const };
  const [design] = artifactDesigns([old, current, observation]);
  assert.equal(design?.latest.id, 'current');
  assert.equal(design?.latest.preview, current);
  const oldVersion = design?.versions.find(({ id }) => id === 'old');
  assert.equal(oldVersion?.modifiedAt, old.modifiedAt);
  assert.ok(oldVersion?.artifacts.includes(observation));
});

test('intermediate geometry, reports and legacy attachments do not create design cards', () => {
  const cutter = file('failed', 'feature-cutter.step', { purpose: 'internal-evidence', status: 'failed', primary: false });
  const report = file('failed', 'report.json', { kind: 'report', format: undefined, primary: false });
  const attachment = file('legacy', 'draft.glb', { kind: 'other', format: undefined, legacy: true });
  assert.deepEqual(artifactDesigns([cutter, report, attachment]), []);
  assert.equal(latestArtifactPreview([cutter, report, attachment]), undefined);
});

test('selecting a part or historical plate previews that exact file and build', () => {
  const assembly = file('new', 'new/display.step');
  const part = file('new', 'new/shell.step', { partId: 'shell', primary: false, purpose: 'cad-model' });
  const partStl = { ...part, path: 'new/shell.stl', format: 'stl' as const };
  const oldDisplay = file('old', 'old/display.step', { modifiedAt: '2026-10-15T09:00:00Z' });
  const oldPlate = file('old', 'old/plate.3mf', { plateId: '01', format: '3mf', purpose: 'print-package' });
  const files = [assembly, part, partStl, oldDisplay, oldPlate];
  assert.equal(displayPreviewForSelection(files, part, { displayPreviewPath: assembly.path }), part);
  assert.equal(displayPreviewForSelection(files, oldPlate, undefined), oldDisplay);
  assert.equal(displayPreviewForSelection(files, partStl, undefined), undefined);
  assert.equal(printPreviewForSelection(files, partStl, undefined), partStl);
  assert.equal(printPreviewForSelection(files, oldPlate, undefined), oldPlate);
});
