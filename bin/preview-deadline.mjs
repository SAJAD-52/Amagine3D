export const PREVIEW_DEADLINE_MS = 120_000;
export const PREVIEW_TERMINATION_GRACE_MS = 5_000;

export function armPreviewDeadline(child, {
  deadlineMs = PREVIEW_DEADLINE_MS,
  terminationGraceMs = PREVIEW_TERMINATION_GRACE_MS,
  schedule = setTimeout,
  cancel = clearTimeout,
  onDeadline = () => console.error('a3d agent preview exceeded its 120-second operation deadline'),
} = {}) {
  let timedOut = false;
  let forceTimer;
  const timer = schedule(() => {
    timedOut = true;
    onDeadline();
    child.kill('SIGTERM');
    forceTimer = schedule(
      () => child.kill(process.platform === 'win32' ? undefined : 'SIGKILL'),
      terminationGraceMs,
    );
    forceTimer.unref?.();
  }, deadlineMs);
  timer.unref?.();
  return {
    didTimeout: () => timedOut,
    clear: () => {
      cancel(timer);
      if (forceTimer) cancel(forceTimer);
    },
  };
}
