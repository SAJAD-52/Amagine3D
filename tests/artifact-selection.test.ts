import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import {
  defaultPreviewArtifact,
  fileSectionArtifacts,
  preferredDisplayPreviewArtifact,
  preferredPrintPreviewArtifact,
  printPlatePreviews,
  printPreviewForSelection,
} from '../src/lib/artifact-selection.ts';
import type { ArtifactSummary, PreviewFormat } from '../src/types.ts';

function artifact(
  path: string,
  kind: ArtifactSummary['kind'],
  modifiedAt = '2026-08-23T08:00:00.000Z',
): ArtifactSummary {
  return {
    kind,
    modifiedAt,
    name: path.split('/').at(-1) ?? path,
    path,
    size: 1,
    url: `/api/artifacts/file?path=${encodeURIComponent(path)}`,
  };
}

function model(
  path: string,
  format: PreviewFormat | undefined,
  modifiedAt: string,
): ArtifactSummary {
  return {
    ...artifact(path, 'model', modifiedAt),
    ...(format ? { purpose: format === 'step' ? 'display-preview' as const : 'print-package' as const } : {}),
    ...(format ? { format } : {}),
  };
}

test('keeps plate selection and hides individual parts and obsolete plates', () => {
  const display = { ...model('pair-display.step', 'step', '2026-09-09'), primary: true, modelId: 'pair', buildId: 'new' };
  const plates = ['01', '02'].flatMap((plateId) => (['3mf', 'stl'] as const).map((format) => ({
    ...model(`pair-plate-${plateId}.${format}`, format, `2026-09-09T00:00:0${plateId === '02' ? 2 : 1}Z`),
    primary: true, modelId: 'pair', buildId: 'new', plateId,
  })));
  const artifacts = [
    model('pair-part-a.stl', 'stl', '2026-09-10'),
    model('pair-plate-03.stl', 'stl', '2026-09-10'),
    ...plates.slice().reverse(), display,
  ];
  assert.deepEqual(fileSectionArtifacts(artifacts).map(({ path }) => path), [display.path, ...plates.map(({ path }) => path)]);
  assert.equal(preferredPrintPreviewArtifact(artifacts)?.path, plates[0]!.path);
  for (const selected of plates) {
    assert.equal(printPreviewForSelection(artifacts, selected, { primaryPreviewPath: plates[0]!.path }), selected);
  }
  assert.equal(printPreviewForSelection(artifacts, display, { primaryPreviewPath: plates[0]!.path }), plates[0]);
});

test('lists non-primary observation images without making them default previews', () => {
  const assembly = { ...model('assembly.step', 'step', '2026-10-22'),
    primary: true, modelId: 'assembly', buildId: 'run-1' };
  const observation = { ...artifact('observation-00.png', 'image'), purpose: 'observation-preview' as const,
    primary: false, modelId: 'assembly', buildId: 'run-1', inputsCurrent: true };
  assert.equal(defaultPreviewArtifact([observation, assembly]), assembly);
  assert.deepEqual(fileSectionArtifacts([observation, assembly]).map(({ path }) => path), [assembly.path, observation.path]);
});

test('lists each plate once in numeric order and prefers its 3MF preview', () => {
  const plates = ['10', '02', '01'].flatMap((plateId) => (['stl', '3mf'] as const).map((format) => ({
    ...model(`pair-plate-${plateId}.${format}`, format, '2026-10-15'),
    buildId: 'current', designId: 'design:pair', modelId: 'pair', plateId,
  })));
  const current = plates.find(({ path }) => path === 'pair-plate-01.3mf')!;
  assert.deepEqual(printPlatePreviews(plates, current).map(({ path }) => path), [
    'pair-plate-01.3mf', 'pair-plate-02.3mf', 'pair-plate-10.3mf',
  ]);
  const stlOnly = plates.filter(({ path }) => path !== 'pair-plate-02.3mf');
  assert.equal(printPlatePreviews(stlOnly, current)[1]?.format, 'stl');
  const selectedStl = plates.find(({ path }) => path === 'pair-plate-02.stl')!;
  assert.equal(printPlatePreviews(plates, selectedStl)[1], selectedStl);
});

test('plate navigation stays within the selected design and build, including history', () => {
  const plate = (path: string, buildId: string, plateId: string): ArtifactSummary => ({
    ...model(path, '3mf', '2026-10-15'), buildId, plateId, designId: 'design:pair', modelId: 'pair',
  });
  const current = plate('current/plate-01.3mf', 'current', '01');
  const second = plate('current/plate-02.3mf', 'current', '02');
  const history = plate('history/plate-01.3mf', 'history', '01');
  const historySecond = plate('history/plate-02.3mf', 'history', '02');
  const artifacts = [
    current, second, history, historySecond,
    { ...plate('other-design.3mf', 'current', '03'), designId: 'design:other' },
    { ...plate('other-model.3mf', 'current', '04'), modelId: 'other' },
    { ...plate('part.3mf', 'current', '05'), partId: 'shell' },
    { ...plate('failed.3mf', 'current', '06'), status: 'failed' as const },
    { ...plate('draft.3mf', 'current', '07'), status: 'draft' as const },
    { ...plate('internal.3mf', 'current', '08'), purpose: 'internal-evidence' as const },
  ];
  assert.deepEqual(printPlatePreviews(artifacts, current), [current, second]);
  assert.deepEqual(printPlatePreviews(artifacts, historySecond), [history, historySecond]);
});

test('does not invent plate groups for parts, drafts or unscoped legacy files', () => {
  const legacy = model('legacy.3mf', '3mf', '2026-10-15');
  const other = model('other.3mf', '3mf', '2026-10-15');
  assert.deepEqual(printPlatePreviews([legacy, other], legacy), [legacy]);
  const withoutBuild = { ...legacy, plateId: '01' };
  assert.deepEqual(printPlatePreviews([withoutBuild, other], withoutBuild), [withoutBuild]);
  assert.deepEqual(printPlatePreviews([], undefined), []);
  assert.deepEqual(printPlatePreviews([legacy], { ...legacy, partId: 'shell' }), []);
  assert.deepEqual(printPlatePreviews([legacy], { ...legacy, status: 'draft' }), []);
});

test('failed-build cutters and unknown files never become automatic product previews', () => {
  const cutter = { ...model('design-builds/failed/feature-cap-socket.stl', 'stl', '2026-10-13'),
    purpose: 'internal-evidence' as const };
  const unknown = { ...model('unknown.step', 'step', '2026-10-13'), purpose: 'unclassified' as const };
  assert.equal(defaultPreviewArtifact([cutter, unknown]), undefined);
  assert.equal(preferredPrintPreviewArtifact([cutter]), undefined);
  assert.deepEqual(fileSectionArtifacts([cutter, unknown]), []);
  const draft = { ...model('.amagine3d-drafts/current/draft.step', 'step', '2026-10-13'),
    purpose: 'draft-preview' as const, status: 'draft' as const, primary: true };
  assert.equal(defaultPreviewArtifact([cutter, unknown, draft]), draft);
  assert.equal(preferredPrintPreviewArtifact([draft, cutter]), undefined);
});

test('selects the display STEP over the top-level STL for single-color builds', () => {
  const artifacts = [
    model('bracket.stl', 'stl', '2026-08-23T08:00:02.000Z'),
    model('bracket-display.step', 'step', '2026-08-23T08:00:03.000Z'),
  ];
  assert.equal(
    preferredDisplayPreviewArtifact(artifacts)?.path,
    'bracket-display.step',
  );
  assert.equal(defaultPreviewArtifact(artifacts)?.path, 'bracket-display.step');
});

test('selects the display STEP over the 3MF print package for color builds', () => {
  const artifacts = [
    model('timer-display.step', 'step', '2026-08-23T08:00:05.000Z'),
    model('timer.3mf', '3mf', '2026-08-23T08:00:04.000Z'),
    model('timer-region-screen.stl', 'stl', '2026-08-23T08:00:03.000Z'),
    model('timer-region-housing.stl', 'stl', '2026-08-23T08:00:02.000Z'),
  ];
  assert.equal(
    preferredDisplayPreviewArtifact(artifacts)?.path,
    'timer-display.step',
  );
  assert.equal(preferredPrintPreviewArtifact(artifacts)?.path, 'timer.3mf');
});

test('honors a featured display STEP as the visible model', () => {
  const artifacts = [
    model('timer.3mf', '3mf', '2026-08-23T08:00:04.000Z'),
    {
      ...model('timer-display.step', 'step', '2026-08-23T08:00:03.000Z'),
      featured: true,
    },
  ];
  assert.equal(
    preferredDisplayPreviewArtifact(artifacts)?.path,
    'timer-display.step',
  );
});

test('does not let an older multi-color print package override a newer STL build', () => {
  const artifacts = [
    model('new-part.stl', 'stl', '2026-08-23T09:00:00.000Z'),
    model('old-part.3mf', '3mf', '2026-08-23T08:00:00.000Z'),
  ];
  assert.equal(preferredPrintPreviewArtifact(artifacts)?.path, 'new-part.stl');
});

test('prefers 3MF when top-level print outputs share the same timestamp', () => {
  const artifacts = [
    model('timer.stl', 'stl', '2026-08-23T08:00:00.000Z'),
    model('timer.3mf', '3mf', '2026-08-23T08:00:00.000Z'),
  ];
  assert.equal(preferredPrintPreviewArtifact(artifacts)?.path, 'timer.3mf');
});

test('honors the explicit preview of a bundled project', () => {
  const featured = {
    ...model('focus-bar-logical-assembly.3mf', '3mf', '2026-01-01T00:00:00.000Z'),
    featured: true,
  };
  const artifacts = [
    model('timer-knob-orange.stl', 'stl', '2026-08-23T08:00:00.000Z'),
    featured,
  ];
  assert.equal(
    defaultPreviewArtifact(artifacts)?.path,
    'focus-bar-logical-assembly.3mf',
  );
});

test('shows STEP, 3MF, and only the matching top-level STL in the file section', () => {
  const artifacts = [
    artifact('part.py', 'source'),
    {
      ...model('part-display.step', 'step', '2026-08-23T08:00:05.000Z'),
      primary: true,
    },
    {
      ...model('part.3mf', '3mf', '2026-08-23T08:00:04.500Z'),
      primary: true,
    },
    {
      ...model('part.stl', 'stl', '2026-08-23T08:00:04.250Z'),
      primary: true,
    },
    model('part-shell.stl', 'stl', '2026-08-23T08:00:04.100Z'),
    model('part-assemble.step', undefined, '2026-08-23T08:00:04.000Z'),
    artifact('preview.PNG', 'image'),
    artifact('reference.webp', 'image'),
    artifact('part_report.json', 'report'),
  ];
  assert.deepEqual(
    fileSectionArtifacts(artifacts).map(({ path }) => path),
    ['part-display.step', 'part.3mf', 'part.stl'],
  );
});

test('pins the preferred visible model at the top of the file section', () => {
  const preferred = {
    ...model('shell_case-display.step', 'step', '2026-08-23T08:00:01.000Z'),
    featured: true,
    primary: true,
  };
  const artifacts = [
    {
      ...model('shell_case.stl', 'stl', '2026-08-23T08:00:05.000Z'),
      primary: true,
    },
    artifact('preview.png', 'image', '2026-08-23T08:00:04.000Z'),
    model('shell_case-top-lid.stl', 'stl', '2026-08-23T08:00:03.000Z'),
    preferred,
  ];
  assert.deepEqual(
    fileSectionArtifacts(artifacts).map(({ path }) => path),
    [
      'shell_case-display.step',
      'shell_case.stl',
    ],
  );
});

test('uses valid-build primary metadata to hide orphaned model revisions', () => {
  const artifacts = [
    {
      ...model('ninekey-display.step', 'step', '2026-08-23T09:00:00.000Z'),
      featured: true,
      primary: true,
    },
    {
      ...model('ninekey.3mf', '3mf', '2026-08-23T09:00:00.000Z'),
      primary: true,
    },
    {
      ...model('ninekey.stl', 'stl', '2026-08-23T09:00:00.000Z'),
      primary: true,
    },
    model('relocated/a.step', 'step', '2026-08-23T08:00:00.000Z'),
    model('relocated/b.3mf', '3mf', '2026-08-23T08:00:00.000Z'),
    model('relocated/c.stl', 'stl', '2026-08-23T08:00:00.000Z'),
  ];
  assert.deepEqual(
    fileSectionArtifacts(artifacts).map(({ path }) => path),
    ['ninekey-display.step', 'ninekey.3mf', 'ninekey.stl'],
  );
});
