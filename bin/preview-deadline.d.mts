import type { ChildProcess } from 'node:child_process';

export const PREVIEW_DEADLINE_MS: number;
export const PREVIEW_TERMINATION_GRACE_MS: number;
export function armPreviewDeadline(
  child: Pick<ChildProcess, 'kill'>,
  options?: {
    deadlineMs?: number;
    terminationGraceMs?: number;
    schedule?: typeof setTimeout;
    cancel?: typeof clearTimeout;
    onDeadline?: () => void;
  },
): { didTimeout(): boolean; clear(): void };
