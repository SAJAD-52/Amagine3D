import type { ArtifactSummary } from '../types';
import { defaultPreviewArtifact, fileSectionArtifacts, printableArtifact } from './artifact-selection';

export interface ArtifactVersion {
  id: string;
  number: number;
  status: 'draft' | 'validated' | 'diagnostic' | 'unverified';
  modifiedAt: string;
  inputsCurrent: boolean;
  artifacts: ArtifactSummary[];
  preview?: ArtifactSummary;
  thumbnailUrl?: string;
  cad?: ArtifactSummary;
  prints: ArtifactSummary[];
  parts: { id: string; artifacts: ArtifactSummary[] }[];
  plates: { id: string; artifacts: ArtifactSummary[] }[];
}

export interface ArtifactDesign {
  id: string;
  name: string;
  versions: ArtifactVersion[];
  latest: ArtifactVersion;
  draft?: ArtifactVersion;
  validated?: ArtifactVersion;
}

function timestamp(artifact: { modifiedAt: string }): number {
  return Date.parse(artifact.modifiedAt) || 0;
}

function designKey(artifact: ArtifactSummary): string {
  return artifact.designId ?? (artifact.modelId ? `model:${artifact.modelId}` : 'workspace');
}

function versionKey(artifact: ArtifactSummary): string {
  return artifact.buildId ?? artifact.revision ?? 'unversioned';
}

function childGroups(artifacts: ArtifactSummary[], key: 'partId' | 'plateId') {
  const groups = new Map<string, ArtifactSummary[]>();
  for (const artifact of artifacts) {
    const id = artifact[key];
    if (!id || artifact.purpose === 'internal-evidence') continue;
    const files = groups.get(id) ?? [];
    files.push(artifact);
    groups.set(id, files);
  }
  return [...groups].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
    .map(([id, files]) => ({ id, artifacts: files }));
}

function version(id: string, artifacts: ArtifactSummary[]): ArtifactVersion {
  const roots = fileSectionArtifacts(artifacts).filter((artifact) =>
    artifact.kind !== 'other' && artifact.purpose !== 'observation-preview');
  const statuses = roots.map((artifact) => artifact.status);
  const status = statuses.some((value) => value === 'failed' || value === 'diagnostic') ? 'diagnostic'
    : statuses.includes('draft') ? 'draft'
      : statuses.includes('validated') ? 'validated' : 'unverified';
  const preview = defaultPreviewArtifact(roots) ?? roots.find((artifact) =>
    artifact.kind === 'image' && artifact.purpose !== 'observation-preview') ?? roots[0];
  const cad = roots.find((artifact) => artifact.purpose === 'cad-model' && !artifact.partId)
    ?? roots.find((artifact) => artifact.format === 'step' && !artifact.partId);
  return {
    id, number: 0, status,
    modifiedAt: [...roots].sort((a, b) => timestamp(b) - timestamp(a))[0]?.modifiedAt ?? '',
    inputsCurrent: roots.some((artifact) => artifact.inputsCurrent !== false),
    artifacts, preview, cad,
    thumbnailUrl: preview?.previewImageUrl ?? roots.find((artifact) =>
      artifact.kind === 'image' && artifact.purpose !== 'observation-preview')?.url,
    prints: roots.filter(printableArtifact),
    parts: childGroups(artifacts, 'partId'),
    plates: childGroups(artifacts.filter(printableArtifact), 'plateId'),
  };
}

/** One design card owns all its build revisions and declared part/plate files. */
export function artifactDesigns(artifacts: readonly ArtifactSummary[], fallbackName = ''): ArtifactDesign[] {
  const roots = fileSectionArtifacts(artifacts).filter((artifact) =>
    artifact.kind !== 'other' && artifact.purpose !== 'observation-preview');
  const groups = new Map<string, Map<string, ArtifactSummary[]>>();
  for (const artifact of roots) {
    const key = designKey(artifact);
    const versions = groups.get(key) ?? new Map<string, ArtifactSummary[]>();
    if (!versions.has(versionKey(artifact))) versions.set(versionKey(artifact), []);
    groups.set(key, versions);
  }
  for (const artifact of artifacts) {
    groups.get(designKey(artifact))?.get(versionKey(artifact))?.push(artifact);
  }
  return [...groups].map(([id, builds]) => {
    const versions = [...builds].map(([buildId, files]) => version(buildId, files))
      .sort((a, b) => timestamp(b) - timestamp(a) || a.id.localeCompare(b.id));
    versions.forEach((item, index) => { item.number = versions.length - index; });
    const validated = versions.find((item) => item.status === 'validated');
    // Once a newer build is validated, its superseded drafts belong in history.
    const draft = versions.find((item) => item.status === 'draft' && (!validated || timestamp(item) > timestamp(validated)));
    const latest = [draft, validated].filter((item): item is ArtifactVersion => Boolean(item))
      .sort((a, b) => timestamp(b) - timestamp(a))[0] ?? versions.find((item) => item.status !== 'diagnostic') ?? versions[0]!;
    const files = versions.flatMap((item) => item.artifacts);
    const modelName = files.find((artifact) => artifact.modelId)?.modelId;
    const sourceName = id.startsWith('source:') || id.startsWith('design:')
      ? id.split('/').at(-1)!.replace(/^(?:source|design):/u, '').replace(/(?:\.a3d)?\.(?:json|py|mjs|js)$/u, '') : undefined;
    return { id, name: modelName ?? (groups.size === 1 && fallbackName ? fallbackName : sourceName ?? fallbackName),
      versions, latest, draft, validated };
  }).sort((a, b) => timestamp(b.latest) - timestamp(a.latest) || a.id.localeCompare(b.id));
}

/** Manual history/part selection always stays attached to its exact build. */
export function selectedDesignVersion(design: ArtifactDesign, selectedPath?: string): ArtifactVersion {
  return design.versions.find((item) => item.artifacts.some((artifact) => artifact.path === selectedPath)) ?? design.latest;
}
