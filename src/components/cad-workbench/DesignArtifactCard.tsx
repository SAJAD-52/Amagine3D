import { useState } from 'react';

import { selectedDesignVersion, type ArtifactDesign, type ArtifactVersion } from '../../lib/artifact-groups';
import type { ArtifactSummary } from '../../types';
import { ArtifactIcon } from './ArtifactIcon';
import styles from './FilesPanel.module.css';
import { translator, type Language } from './types';

interface FileListProps {
  language: Language;
  selectedPath?: string;
  selectedPaths: Set<string>;
  onSelect: (artifact: ArtifactSummary) => void;
  onToggle: (path: string) => void;
}

export function ArtifactFileList({ artifacts, language, selectedPath, selectedPaths, onSelect, onToggle }:
  FileListProps & { artifacts: ArtifactSummary[] }) {
  const text = translator(language);
  return <ul className={styles.fileTree}>
    {artifacts.map((artifact) => <li key={artifact.path}>
      <input aria-label={text(`Select ${artifact.name}`, `选择 ${artifact.name}`)} checked={selectedPaths.has(artifact.path)}
        className={styles.fileTreeCheckbox} onChange={() => onToggle(artifact.path)} type="checkbox" />
      <button aria-current={selectedPath === artifact.path} onClick={() => onSelect(artifact)} title={artifact.path} type="button">
        <ArtifactIcon artifact={artifact} selected={selectedPath === artifact.path} size="compact" />
        <span>{artifact.name}</span>
      </button>
    </li>)}
  </ul>;
}

export function DesignArtifactCard({ design, downloading, onDownload, ...listProps }: FileListProps & {
  design: ArtifactDesign;
  downloading: boolean;
  onDownload: (artifacts: ArtifactSummary[]) => void;
}) {
  const { language, selectedPath, onSelect } = listProps;
  const text = translator(language);
  const active = selectedDesignVersion(design, selectedPath);
  const [brokenThumbnail, setBrokenThumbnail] = useState<string>();
  const currentVersions = [design.draft, design.validated].filter((item): item is ArtifactVersion => Boolean(item));
  if (!currentVersions.length) currentVersions.push(design.latest);
  const history = design.versions;
  const statusLabel = (item: ArtifactVersion) => item.status === 'draft' ? text('Draft', '草稿')
    : item.status === 'validated' ? text('Validated', '已验证')
      : item.status === 'diagnostic' ? text('Unresolved checks', '未通过检查') : text('Preview', '预览');
  const selectVersion = (item: ArtifactVersion) => { if (item.preview) onSelect(item.preview); };
  const hasNewer = active.id !== design.latest.id;
  const date = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US',
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';
  const previewImage = active.thumbnailUrl && active.thumbnailUrl !== brokenThumbnail ? active.thumbnailUrl : undefined;
  const printDownload = active.plates.length ? active.plates.flatMap((plate) =>
    plate.artifacts.find((artifact) => artifact.format === '3mf') ?? plate.artifacts[0] ?? [])
    : active.prints.filter((artifact) => artifact.format === (active.prints.some((file) => file.format === '3mf') ? '3mf' : 'stl'));
  const files = active.artifacts.filter((artifact) =>
    artifact.primary || artifact.partId || artifact.plateId || artifact.purpose === 'observation-preview');

  return <article className={styles.designCard} aria-label={design.name || text('Design', '设计成果')}>
    <header className={styles.designHeader}>
      <h3 title={design.name}>{design.name || text('Design', '设计成果')}</h3>
      <span className={styles.statusBadge} data-status={active.status}>{statusLabel(active)}</span>
    </header>
    {currentVersions.length > 1 ? <div className={styles.versionTabs} aria-label={text('Design version', '设计版本')}>
      {currentVersions.map((item) => <button key={item.id} type="button" aria-pressed={active.id === item.id} onClick={() => selectVersion(item)}>
        {item === design.draft ? text('Latest draft', '最新草稿') : text('Validated version', '已验证版')}
      </button>)}
    </div> : null}
    <button className={styles.designPreview} type="button" disabled={!active.preview} onClick={() => selectVersion(active)}
      aria-label={text(`Preview ${design.name}, version ${active.number}`, `预览 ${design.name}，第 ${active.number} 版`)}>
      {previewImage ? <img src={previewImage} alt={text(`${design.name} assembly preview`, `${design.name} 整机预览`)}
        onError={() => setBrokenThumbnail(previewImage)} /> : <span className={styles.previewPlaceholder}>
        <svg viewBox="0 0 64 64" fill="none" aria-hidden="true"><path d="m32 7 23 13v25L32 58 9 45V20L32 7Z M9 20l23 13 23-13 M32 33v25" /></svg>
        <span>{text('View assembly', '查看整机')}</span>
      </span>}
      <span className={styles.previewVersion}>v{active.number}</span>
    </button>
    <div className={styles.versionMeta}>
      <span>{date(active.modifiedAt)}</span>
      {!active.inputsCurrent ? <span>{text('Previous inputs', '对应此前的设计')}</span> : null}
    </div>
    {active.status === 'draft' ? <p className={styles.versionNote}>{text('Preview ready. Final checks are still pending.', '草稿已可预览，尚未完成最终检查。')}</p>
      : active.status === 'diagnostic' ? <p className={styles.versionNote}>{text('Some checks failed. This version is for inspection.', '此版本有未通过项，可查看模型排查问题。')}</p> : null}
    {design.versions[0]?.status === 'diagnostic' && active.id !== design.versions[0].id ?
      <p className={styles.versionNote}>{text('The latest attempt failed. Keeping the last available preview.', '最新尝试未通过检查，已保留上次可用预览。')}</p> : null}
    {hasNewer ? <button className={styles.newVersion} type="button" onClick={() => selectVersion(design.latest)}>
      {text('View latest preview', '查看最新预览')} <span aria-hidden="true">→</span>
    </button> : null}
    <div className={styles.designActions}>
      {active.cad ? <button type="button" disabled={downloading} onClick={() => onDownload([active.cad!])}>
        {active.status === 'draft' ? text('Download draft STEP', '下载草稿 STEP') : text('Download STEP', '下载 STEP')}
      </button> : null}
      {printDownload.length > 0 ? <button type="button" disabled={downloading} onClick={() => onDownload(printDownload)}>
        {text('Download print package', '下载打印包')}
      </button> : null}
    </div>
    {active.plates.length > 0 ? <details className={styles.cardDetails} key={`${active.id}:plates`}>
      <summary>{text('Print plates', '打印盘')} <span>{active.plates.length}</span></summary>
      {active.plates.map((plate) => <div className={styles.childGroup} key={plate.id}>
        <h4>{text(`Plate ${Number(plate.id)}`, `第 ${Number(plate.id)} 盘`)}</h4><ArtifactFileList artifacts={plate.artifacts} {...listProps} />
      </div>)}
    </details> : null}
    {active.parts.length > 0 ? <details className={styles.cardDetails} key={`${active.id}:parts`}>
      <summary>{text('Parts', '子部件')} <span>{active.parts.length}</span></summary>
      {active.parts.map((part) => <div className={styles.childGroup} key={part.id}>
        <h4>{part.id}</h4><ArtifactFileList artifacts={part.artifacts} {...listProps} />
      </div>)}
    </details> : null}
    <details className={styles.cardDetails} key={`${active.id}:files`}>
      <summary>{text('Version files', '本版文件')} <span>{files.length}</span></summary>
      <ArtifactFileList artifacts={files} {...listProps} />
    </details>
    {history.length > 0 ? <details className={styles.cardDetails}>
      <summary>{text('Version history', '历史版本')} <span>{history.length}</span></summary>
      <ul className={styles.versionHistory}>{history.map((item) => <li key={item.id}>
        <button type="button" aria-current={active.id === item.id} onClick={() => selectVersion(item)}>
          <span>v{item.number} · {statusLabel(item)}{item === history[0] ? <span className={styles.latestVersion}>{text('Latest', '最新')}</span> : null}</span>
          <time dateTime={item.modifiedAt}>{date(item.modifiedAt)}</time>
        </button>
      </li>)}</ul>
    </details> : null}
  </article>;
}
