// Thin client for /admin/api. The session cookie is HttpOnly + SameSite=Strict;
// every request is JSON, which a cross-site form cannot send.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: {} };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const resp = await fetch(`/admin/api${path}`, init);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = typeof data.detail === "string" ? data.detail : data.detail?.message ?? resp.statusText;
    throw new ApiError(resp.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T,>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T,>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  del: <T,>(path: string) => request<T>("DELETE", path),
};

// ---- shapes returned by the back end ---------------------------------------

export interface Status {
  version: string;
  needs_setup: boolean;
  pack: string;
  packs: { name: string; label: string; description: string }[];
  callback_url: string;
  model_configured: boolean;
}

export interface AuthParam {
  name: string;
  label: string;
  description: string;
  required: boolean;
  secret: boolean;
  default: string | null;
}

export interface AuthMethod {
  method: string;
  label: string;
  oauth: boolean;
  help: string;
  params: AuthParam[];
}

export interface SystemOption {
  preset: string;
  label: string;
  description: string;
  provides: string[];
  setup_notes: string;
  auth_methods: AuthMethod[];
}

export interface Category {
  key: string;
  label: string;
  description: string;
  entities: string[];
  required: boolean;
  systems: SystemOption[];
  configured_sources: string[];
}

export interface Catalog {
  pack: { name: string; label: string; description: string; terminology: Record<string, string> };
  packs: { name: string; label: string; description: string }[];
  categories: Category[];
  generic: { type: string; label: string; description: string }[];
  callback_url: string;
}

export interface SourceInfo {
  name: string;
  preset: string | null;
  type: string;
  auth_method: string | null;
  entities: string[];
  primary: boolean;
  test_data: boolean;
  connection: { credentials_complete: boolean; missing: string[]; oauth_connected?: boolean };
  last_test: { ok: boolean; detail: string; at: number } | null;
}

export interface SaveResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
  note?: string;
}

export interface FieldDoc {
  type: string;
  description: string;
  aliases: string[];
}

export interface SourceField {
  name: string;
  label?: string;
  description?: string;
  type?: string | null;
  options?: string[];
  examples: string[];
  free_text: boolean;
}

export type FieldSpec = string | number | boolean | null | { expr: string; map?: Record<string, string> };

export interface Suggestion {
  field: string;
  expr: FieldSpec;
  source_field: string | null;
  confidence: number;
  reason: string;
  by: string;
  warnings: string[];
  auto_accept: boolean;
}

export interface MappingView {
  source: string;
  entity: string;
  entity_description: string;
  standard: Record<string, FieldDoc>;
  current: Record<string, FieldSpec>;
  read_only: boolean;
  describe: { fields: SourceField[]; samples: Record<string, string | null>[]; sample_count: number };
  signals_affected: Record<string, string[]>;
}

export interface SuggestResult {
  suggestions: Suggestion[];
  unmapped: string[];
  required_missing: string[];
  signals_affected: Record<string, string[]>;
  note: string | null;
}

export interface LinkSource {
  method: string;
  error: string | null;
  linked: number;
  coverage: number;
  customers: number;
  review: { account_id: string; account_name?: string; customer_id: string; customer_name?: string; score: number | null; reason: string }[];
  unmatched_customers: { id: string; name?: string }[];
}

export interface Linking {
  accounts: number;
  sources: Record<string, LinkSource>;
  note?: string;
  rules?: Record<string, unknown[]>;
}

export interface SignalRow {
  name: string;
  description: string;
  specialist: string;
  enabled: boolean;
  thresholds: Record<string, number>;
  direction: string;
  entities: string[];
  has_data: boolean;
}
