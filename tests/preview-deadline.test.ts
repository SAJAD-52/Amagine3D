import { strict as assert } from 'node:assert';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { test } from 'node:test';
import {
  armPreviewDeadline,
  PREVIEW_DEADLINE_MS,
  PREVIEW_TERMINATION_GRACE_MS,
} from '../bin/preview-deadline.mjs';

test('preview deadline requests graceful termination then force-kills after its grace period', () => {
  const timers: { callback: () => void; delay: number; cleared: boolean; unref(): void }[] = [];
  const schedule = ((callback: () => void, delay?: number) => {
    const timer = { callback, delay: delay ?? 0, cleared: false, unref() {} };
    timers.push(timer);
    return timer;
  }) as unknown as typeof setTimeout;
  const cancel = ((timer: unknown) => {
    const entry = timer as (typeof timers)[number];
    entry.cleared = true;
  }) as typeof clearTimeout;
  const signals: (NodeJS.Signals | number | undefined)[] = [];
  let warningCount = 0;
  const deadline = armPreviewDeadline({ kill: (signal) => { signals.push(signal); return true; } }, {
    schedule, cancel, onDeadline: () => { warningCount += 1; },
  });

  assert.equal(timers[0]?.delay, PREVIEW_DEADLINE_MS);
  assert.equal(deadline.didTimeout(), false);
  timers[0]?.callback();
  assert.equal(deadline.didTimeout(), true);
  assert.equal(warningCount, 1);
  assert.deepEqual(signals, ['SIGTERM']);
  assert.equal(timers[1]?.delay, PREVIEW_TERMINATION_GRACE_MS);
  timers[1]?.callback();
  assert.deepEqual(signals, ['SIGTERM', process.platform === 'win32' ? undefined : 'SIGKILL']);
  deadline.clear();
  assert.equal(timers.every((timer) => timer.cleared), true);
});

test('short injected deadline terminates a real child that ignores graceful shutdown', async () => {
  const child = spawn(process.execPath, ['-e',
    "if (process.platform !== 'win32') process.on('SIGTERM', () => {}); console.log('READY'); setInterval(() => {}, 1000);"]
  , { stdio: ['ignore', 'pipe', 'ignore'] });
  const lines = createInterface({ input: child.stdout });
  let deadline: ReturnType<typeof armPreviewDeadline> | undefined;
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('child did not become ready')), 5_000);
      lines.once('line', (line) => {
        clearTimeout(timer);
        if (line === 'READY') resolve();
        else reject(new Error(`unexpected child readiness: ${line}`));
      });
      child.once('error', reject);
    });
    deadline = armPreviewDeadline(child, { deadlineMs: 50, terminationGraceMs: 50, onDeadline() {} });
    const result = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('deadline failed to terminate the child')), 5_000);
      child.once('error', reject);
      child.once('exit', (code, signal) => {
        clearTimeout(timer);
        resolve({ code, signal });
      });
    });
    assert.equal(deadline.didTimeout(), true);
    if (process.platform !== 'win32') assert.equal(result.signal, 'SIGKILL');
    else assert.ok(result.code !== null || result.signal !== null);
  } finally {
    deadline?.clear();
    lines.close();
    if (child.exitCode === null && child.signalCode === null) child.kill();
  }
});
