import type {
  CadSystem,
  CadSystemAvailability,
  TurnUsage,
} from '@amagine3d/a3d-runtime';

export const API_VERSION = 12;
export const BUNDLED_POMODORO_SESSION_ID = 'builtin:amagine3d-pomodoro';
export type { CadSystem, CadSystemAvailability, TurnUsage };
export const ACCEPTED_IMAGE_TYPES = [
  'image/png',
  'image/jpeg',
  'image/webp',
  'image/gif',
] as const;
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
export const MAX_IMAGE_COUNT = 4;
export const MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024;

export type AcceptedImageType = (typeof ACCEPTED_IMAGE_TYPES)[number];
export type ArtifactKind = 'image' | 'model' | 'report' | 'source' | 'other';
export type ChatTaskType = 'cad' | 'chat';
export type PreviewFormat = '3mf' | 'step' | 'stl';

export interface ImageAttachment {
  data: string;
  mimeType: AcceptedImageType;
  name: string;
}

export interface ChatImagePreview {
  name: string;
  url: string;
}

export type ChatStepStatus =
  | 'cancelled'
  | 'completed'
  | 'failed'
  | 'running';

export type ChatTurnTerminalStatus = Exclude<ChatStepStatus, 'running'>;

export interface LocalizedText {
  en: string;
  zh: string;
}

export interface ChatStep {
  id: string;
  label: string;
  localizedLabel?: LocalizedText;
  occurredAt: number;
  progressText?: string;
  stage: string;
  status: ChatStepStatus;
}

/**
 * How one runtime execution attempt ended. This describes the model execution
 * only; a completed attempt is not evidence that CAD acceptance passed.
 */
export type TurnAccountingOutcome =
  | 'cancelled'
  | 'completed'
  | 'failed'
  | 'not_started'
  | 'timed_out';

/**
 * Whether the SDK reported the complete five counts, only some of them, or
 * nothing at all for this attempt.
 */
export type TurnUsageCoverage = 'full' | 'partial' | 'unavailable';

/**
 * Optional bookkeeping for one `runTurn` execution attempt. Never infer counts
 * the SDK did not report: a missing field is unknown, not zero.
 */
export interface ChatTurnAccounting {
  attemptId: string;
  model: string;
  outcome: TurnAccountingOutcome;
  taskType: ChatTaskType;
  usage?: TurnUsage;
  usageCoverage: TurnUsageCoverage;
  usageSource: 'sdk';
}

export interface ChatTurn {
  /** Independent from model completion; a CAD pass still requires visual review. */
  cadResult?: CadRunEvidence;
  runtimeFailure?: { code: 'model_output_limit' | 'model_incomplete_response'; eventIndex: number; threadId?: string; completionSeen: boolean };
  /** Absent on turns recorded before per-attempt accounting was added. */
  accounting?: ChatTurnAccounting;
  finishedAt?: number;
  replyText: string;
  /** Absent only on persisted turns created before task-level timing was added. */
  startedAt?: number;
  steps: ChatStep[];
}

export interface CadRunEvidence {
  runId: string;
  report: { path: string; sha256: string };
  status: string;
  pass: boolean;
  visualReviewRequired: boolean;
  inputsCurrent: boolean;
  revision?: string;
  stages?: Record<string, { status: string; [key: string]: unknown }>;
}

export interface UserChatMessage {
  id: string;
  images?: ChatImagePreview[];
  role: 'user';
  text: string;
}

export interface AssistantChatMessage extends ChatTurn {
  id: string;
  role: 'assistant';
}

export type ChatMessage = AssistantChatMessage | UserChatMessage;

export interface SkillSummary {
  description: string;
  name: string;
}

export type ArtifactPurpose = 'display-preview' | 'draft-preview' | 'diagnostic-preview' |
  'observation-preview' | 'print-package' | 'cad-model' | 'internal-evidence' | 'unclassified';

export interface ArtifactSummary {
  /** Stable design/source identity shared by its drafts and subsequent builds. */
  designId?: string;
  /** Hash-bound DSL inputs from the run report. */
  designPath?: string;
  partId?: string;
  legacy?: boolean;
  manufacturingStatus?: string;
  previewImagePath?: string;
  previewImageUrl?: string;
  modelId?: string;
  buildId?: string;
  plateId?: string;
  featured?: boolean;
  format?: PreviewFormat;
  kind: ArtifactKind;
  modifiedAt: string;
  name: string;
  path: string;
  primary?: boolean;
  purpose?: ArtifactPurpose;
  status?: 'draft' | 'diagnostic' | 'failed' | 'validated';
  revision?: string;
  inputsCurrent?: boolean;
  sha256?: string;
  readOnly?: boolean;
  size: number;
  url: string;
}

export interface ArtifactWorkspace {
  id: string;
  name: string;
  path: string;
  readOnly: boolean;
  sessionId: string;
}

export interface ArtifactCollection {
  artifacts: ArtifactSummary[];
  artifactWorkspace: ArtifactWorkspace;
}

export interface ModelParameter {
  id: string;
  kind: 'integer' | 'number';
  label?: { en: string; zh: string };
  maximum?: number;
  minimum?: number;
  step: number;
  unit?: string;
  value: number;
}

export interface ParameterModel {
  runId?: string;
  printPlates?: { id: string; stlPath: string; threeMfPath: string }[];
  artifactPaths: string[];
  displayPreviewPath: string;
  modelId: string;
  parameterError?: string;
  parameters: ModelParameter[];
  primaryPreviewPath: string;
  reportPath: string;
  designPath: string;
  revision: string;
  editable: boolean;
  previewStale?: boolean;
  buildStatus?: 'draft' | 'failed' | 'validated';
}

export interface ParameterCollection {
  models: ParameterModel[];
}

export interface ParameterBuildResult
  extends ArtifactCollection,
    ParameterCollection {}

export type SessionKind = 'builtin' | 'user';

export interface SessionSummary {
  cadSystem?: CadSystem;
  createdAt: string;
  id: string;
  kind: SessionKind;
  persisted: boolean;
  readOnly: boolean;
  title: string;
  updatedAt: string;
}

export interface SessionCatalog {
  initialSessionId: string;
  sessions: SessionSummary[];
}

export interface SessionDetail extends ArtifactCollection {
  messages: ChatMessage[];
  session: SessionSummary;
}

export interface StorageSessionGroup extends ArtifactCollection {
  session: SessionSummary;
}

export interface WorkspaceStorage {
  groups: StorageSessionGroup[];
}

export interface PythonHealth {
  executable: string | null;
  ready: boolean;
  version: string | null;
}

export interface HealthResponse {
  apiVersion: number;
  cadSystems: Record<CadSystem, CadSystemAvailability>;
  configured: boolean;
  model: string;
  python: PythonHealth;
  runtimeError?: string;
  runtimeReady: boolean;
  skills: SkillSummary[];
  searchBackend: 'codex-hosted' | 'disabled' | 'tavily';
  /** Web research can be enabled; health does not invoke its configured backend. */
  webSearchConfigured: boolean;
  webSearchEnabled: boolean;
  webSearchVerification: 'untested';
  workspace: string;
}

export type AgentEvent =
  | { type: 'step'; step: ChatStep }
  | { type: 'step_delta'; content: string; stepId: string }
  | {
      type: 'artifacts';
      artifacts: ArtifactSummary[];
      artifactWorkspace?: ArtifactWorkspace;
      sessionId: string;
      /** Bound to the execution attempt; sequence is monotonic within that turn. */
      attemptId?: string;
      sequence?: number;
    }
  | {
      type: 'complete';
      content: string;
      finishedAt: number;
      sessionId: string;
      sourceStepId?: string;
    }
  | { type: 'error'; message: string; code?: string; finishedAt: number };
