import type { ArtifactSummary } from '../types';

const DISPLAY_PREVIEW_FORMATS = new Set(['step']);
const PRINT_PREVIEW_FORMATS = new Set(['3mf', 'stl']);
const CURRENT_PREVIEW_FORMATS = new Set([
  ...DISPLAY_PREVIEW_FORMATS,
  ...PRINT_PREVIEW_FORMATS,
]);

function modifiedTime(artifact: ArtifactSummary): number {
  const value = Date.parse(artifact.modifiedAt);
  return Number.isFinite(value) ? value : 0;
}

/** Only declared build outputs and explicitly featured bundled models auto-open. */
export function automaticPreviewEligible(artifact: ArtifactSummary): boolean {
  if (artifact.purpose === 'observation-preview') return false;
  if (artifact.partId && !artifact.primary) return false;
  if (artifact.purpose === 'internal-evidence') return false;
  if (artifact.purpose === 'unclassified' && !artifact.primary && !artifact.featured) return false;
  return artifact.kind === 'model' && Boolean(
    artifact.primary || artifact.featured ||
    ['display-preview', 'draft-preview', 'diagnostic-preview', 'print-package'].includes(artifact.purpose ?? ''),
  );
}

export function printableArtifact(artifact: ArtifactSummary): boolean {
  return automaticPreviewEligible(artifact) &&
    artifact.status !== 'draft' && artifact.status !== 'diagnostic' && artifact.status !== 'failed' &&
    (artifact.purpose === undefined || artifact.purpose === 'print-package') &&
    PRINT_PREVIEW_FORMATS.has(artifact.format ?? '');
}

/**
 * Choose the display model produced by the latest CAD build.
 */
export function preferredDisplayPreviewArtifact(
  artifacts: readonly ArtifactSummary[],
): ArtifactSummary | undefined {
  return artifacts
    .filter(
      (artifact) =>
        automaticPreviewEligible(artifact) &&
        artifact.format !== undefined &&
        DISPLAY_PREVIEW_FORMATS.has(artifact.format),
    )
    .sort(
      (left, right) =>
        Number(right.inputsCurrent !== false) - Number(left.inputsCurrent !== false) ||
        (left.buildId && left.buildId === right.buildId
          ? Number(right.purpose !== 'cad-model') - Number(left.purpose !== 'cad-model') : 0) ||
        modifiedTime(right) - modifiedTime(left) ||
        Number(Boolean(right.featured)) - Number(Boolean(left.featured)) ||
        Number(Boolean(right.primary)) - Number(Boolean(left.primary)) ||
        Number(right.path.endsWith('-display.step')) -
          Number(left.path.endsWith('-display.step')) ||
        modifiedTime(right) - modifiedTime(left) ||
        left.path.localeCompare(right.path),
    )[0];
}

/** Choose the latest printable package, preferring 3MF for the same build. */
export function preferredPrintPreviewArtifact(
  artifacts: readonly ArtifactSummary[],
): ArtifactSummary | undefined {
  return artifacts
    .filter(
      (artifact) =>
        printableArtifact(artifact) &&
        artifact.format !== undefined &&
        PRINT_PREVIEW_FORMATS.has(artifact.format),
    )
    .sort(
      (left, right) =>
        Number(right.inputsCurrent !== false) - Number(left.inputsCurrent !== false) ||
        Number(Boolean(right.featured)) - Number(Boolean(left.featured)) ||
        Number(Boolean(right.primary)) - Number(Boolean(left.primary)) ||
        (left.buildId && left.buildId === right.buildId && left.plateId && right.plateId
          ? Number(left.plateId) - Number(right.plateId) || Number(right.format === '3mf') - Number(left.format === '3mf')
          : 0) ||
        modifiedTime(right) - modifiedTime(left) ||
        Number(right.format === '3mf') - Number(left.format === '3mf') ||
        left.path.localeCompare(right.path),
    )[0];
}

export function printPreviewForSelection(
  artifacts: readonly ArtifactSummary[],
  selected: ArtifactSummary | undefined,
  model: { primaryPreviewPath: string } | undefined,
): ArtifactSummary | undefined {
  if (selected?.partId && PRINT_PREVIEW_FORMATS.has(selected.format ?? '')) return selected;
  if (selected && printableArtifact(selected)) return selected;
  if (selected?.status === 'draft' || selected?.status === 'diagnostic' || selected?.status === 'failed') return undefined;
  if (selected?.buildId && !model) return preferredPrintPreviewArtifact(artifacts.filter((artifact) => artifact.buildId === selected.buildId));
  return artifacts.find((artifact) => artifact.path === model?.primaryPreviewPath && printableArtifact(artifact)) ?? preferredPrintPreviewArtifact(artifacts);
}

/** One preview per plate, restricted to the exact design and build being viewed. */
export function printPlatePreviews(
  artifacts: readonly ArtifactSummary[],
  current: ArtifactSummary | undefined,
): ArtifactSummary[] {
  if (!current || current.partId || !printableArtifact(current)) return [];
  if (!current.buildId || !current.plateId) return [current];
  const plates = new Map<string, ArtifactSummary[]>();
  for (const artifact of artifacts) {
    if (!artifact.plateId || artifact.partId || !printableArtifact(artifact) ||
        artifact.buildId !== current.buildId || artifact.designId !== current.designId ||
        artifact.modelId !== current.modelId) continue;
    const files = plates.get(artifact.plateId) ?? [];
    files.push(artifact);
    plates.set(artifact.plateId, files);
  }
  return [...plates]
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
    .map(([, files]) => files.find(({ path }) => path === current.path) ?? preferredPrintPreviewArtifact(files)!);
}

export function displayPreviewForSelection(
  artifacts: readonly ArtifactSummary[],
  selected: ArtifactSummary | undefined,
  model: { displayPreviewPath: string } | undefined,
): ArtifactSummary | undefined {
  if (selected?.format === 'step') return selected;
  if (selected?.partId) return undefined;
  if (model) return artifacts.find((artifact) => artifact.path === model.displayPreviewPath);
  if (selected?.buildId) return preferredDisplayPreviewArtifact(artifacts.filter((artifact) => artifact.buildId === selected.buildId));
  return preferredDisplayPreviewArtifact(artifacts);
}

export function defaultPreviewArtifact(
  artifacts: readonly ArtifactSummary[],
): ArtifactSummary | undefined {
  return (
    preferredDisplayPreviewArtifact(artifacts) ??
    preferredPrintPreviewArtifact(artifacts)
  );
}

function isPreviewModel(artifact: ArtifactSummary): boolean {
  return (
    automaticPreviewEligible(artifact) &&
    artifact.format !== undefined &&
    CURRENT_PREVIEW_FORMATS.has(artifact.format)
  );
}

export function fileSectionArtifacts(
  artifacts: readonly ArtifactSummary[],
): ArtifactSummary[] {
  const preferredPath = defaultPreviewArtifact(artifacts)?.path;
  const groupKey = (artifact: ArtifactSummary) => artifact.buildId
    ? `${artifact.modelId ?? ''}:${artifact.buildId}` : 'legacy';
  const groups = new Map<string, number>();
  for (const artifact of artifacts) {
    if (artifact.primary && !groups.has(groupKey(artifact))) groups.set(groupKey(artifact), groups.size);
  }
  const preferred = artifacts.find(({ path }) => path === preferredPath);
  if (preferred) groups.set(groupKey(preferred), -1);
  return artifacts
    .map((artifact, index) => ({ artifact, index }))
    .filter(
      ({ artifact }) =>
        (artifact.primary === true && (isPreviewModel(artifact) || artifact.purpose === 'cad-model' || artifact.kind === 'image')) ||
        artifact.purpose === 'observation-preview' ||
        (artifact.legacy === true && artifact.kind === 'other'),
    )
    .sort(
      (left, right) =>
        (groups.get(groupKey(left.artifact)) ?? 0) - (groups.get(groupKey(right.artifact)) ?? 0) ||
        Number(right.artifact.path === preferredPath) -
          Number(left.artifact.path === preferredPath) ||
        Number(right.artifact.purpose === 'cad-model') - Number(left.artifact.purpose === 'cad-model') ||
        (left.artifact.buildId && left.artifact.buildId === right.artifact.buildId
          ? Number(left.artifact.plateId ?? 0) - Number(right.artifact.plateId ?? 0) ||
            Number(right.artifact.format === '3mf') - Number(left.artifact.format === '3mf')
          : 0) ||
        left.index - right.index,
    )
    .map(({ artifact }) => artifact);
}
