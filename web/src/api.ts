export type Upload = {
  file_id: string;
  filename: string;
  rows: number;
  columns: string[];
  suggested: Record<string, string | null>;
  fields: string[];
  preview: Record<string, string>[];
  roles?: Record<string, number>;
};

export type RefInfo = {
  inhouse_entities: number;
  name_variants: number;
  variants_by_source: Record<string, number>;
  leis_indexed: number;
  ciq_ids_indexed: number;
  built_in_s: number;
  master_rows?: number;
  gleif_name_index?: boolean;
  gleif_subsidiaries?: number;
  lei_issues?: Record<string, number> | null;
  source?: string;
  universe_file?: string;
  roles?: Record<string, number>;
  published?: Manifest | null;
};

export type MasterSummary = {
  imported_rows: number;
  banks: number;
  last_import: string | null;
  reviewer_decisions: number;
  imported?: number;
  unknown_inhouse_ids?: number;
  note?: string;
  shared_note?: string | null;
};

export type Manifest = {
  file: string;
  sha256: string;
  version: string;
  published_at: string;
  published_by: string;
  roles?: Record<string, number>;
  gleif_publish?: string;
  lei_decisions?: number;
};

export type Conflict = {
  bank_code: string;
  name_clean: string;
  country: string;
  raw_name: string;
  current: string;
  options: { inhouse_id: string; decided_by: string; decided_at: string }[];
};

export type SyncResult = {
  shared: boolean;
  offline: boolean;
  copied: boolean;
  reference_loaded: boolean;
  conflicts: Conflict[];
  decisions?: number;
  users?: number;
  pushed?: number;
  writable?: boolean;
  push_error?: string | null;
  master_rows?: number;
  is_owner?: boolean;
  owner?: string;
};

export type Startup = {
  user: string;
  workspace: string;
  shared: { configured: boolean; path: string; reachable: boolean; owner: string; is_owner: boolean; from_env: boolean };
  reference: { published: Manifest | null; local: Manifest | null; loaded: RefInfo | null; update_available: boolean };
  master: MasterSummary;
  sync: SyncResult | null;
};

export type Job<T> = {
  job_id: string;
  stage: string;
  progress: number | null;
  log: string[];
  done: boolean;
  error: string | null;
  result: T | null;
};

export type Issue = { level: "error" | "warn" | "info"; count: number; text: string; examples: string[] };
export type Validation = { rows: number; ok: boolean; issues: Issue[] };

export type GleifFile = { url: string; size: number; name: string; records?: number; downloaded: boolean };
export type GleifLatest = { publish_date: string; lei2: GleifFile; rr: GleifFile };

export type Built = {
  file: string;
  gleif_publish: string;
  lei_decisions: number;
  sp_ids: boolean;
  summary: { label: string; value: string; sub: boolean }[];
};

export type RunStatus = {
  run_id: string;
  bank_code: string;
  stage: "loading" | "matching" | "done" | "error";
  progress: number;
  summary?: Record<string, number>;
  by_tier?: Record<string, Record<string, number>>;
  seconds?: number;
  error?: string;
  profile?: { fill: Record<string, number>; notes: string[] };
  master?: { rows: number; used: number; unknown_inhouse_id: number };
};

export type ResultRow = {
  bank_row_id: string;
  bank_name: string;
  bank_country: string;
  clean_name: string;
  status: string;
  tier: string;
  evidence: string;
  inhouse_id: string;
  inhouse_name: string;
  score: number | null;
  matched_via: string;
  relation: string;
  inhouse_id_2: string;
  inhouse_name_2: string;
  score_2: number | null;
  inhouse_id_3: string;
  inhouse_name_3: string;
  score_3: number | null;
  margin: number | null;
  decided: string;
};

export type InhouseHit = { inhouse_id: string; name: string; country: string; lei: string };

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not json */
    }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  upload: (
    file: File,
    kind: "inhouse" | "bank" | "ciq" | "master" | "universe" | "period_master" | "period_delivered" | "period_received" | "period_capiq",
    header = true,
  ) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<Upload>(`/api/upload?kind=${kind}&header=${header ? 1 : 0}`, { method: "POST", body: fd });
  },
  validate: (file_id: string, mapping: Record<string, string | null>) =>
    req<Validation>("/api/validate", json({ file_id, mapping })),
  startup: () => req<Startup>("/api/startup"),
  sync: () => req<{ job_id: string }>("/api/startup/sync", { method: "POST" }),
  job: <T,>(id: string) => req<Job<T>>(`/api/jobs/${id}`),
  saveSettings: (shared_folder: string) => req<Startup>("/api/settings", json({ shared_folder })),
  resolve: (c: Conflict, inhouse_id: string) =>
    req<{ conflicts: Conflict[] }>(
      "/api/shared/resolve",
      json({ bank_code: c.bank_code, name_clean: c.name_clean, country: c.country, raw_name: c.raw_name, inhouse_id }),
    ),
  gleifLatest: () => req<GleifLatest>("/api/owner/gleif"),
  ownerBuild: (body: {
    inhouse_file_id: string;
    mapping: Record<string, string | null>;
    sp_ids: boolean;
    lei2_path?: string;
    rr_path?: string;
  }) => req<{ job_id: string }>("/api/owner/build", json(body)),
  ownerPublish: (universe_file_id?: string) => req<Manifest>("/api/owner/publish", json({ universe_file_id })),
  builtFileUrl: "/api/owner/build/file",
  buildReference: (body: {
    inhouse_file_id?: string;
    universe_file_id?: string;
    mapping: Record<string, string | null>;
    gleif_l1_path?: string;
    gleif_rr_path?: string;
    ciq_file_id?: string;
  }) => req<RefInfo>("/api/reference", json(body)),
  reference: () => req<RefInfo | null>("/api/reference"),
  importMaster: (body: { file_id: string; mapping: Record<string, string | null>; replace: boolean }) =>
    req<MasterSummary>("/api/master/import", json(body)),
  master: () => req<MasterSummary>("/api/master"),
  startMatch: (body: Record<string, unknown>) => req<{ run_id: string }>("/api/match", json(body)),
  startPeriod: (body: Record<string, unknown>) => req<{ run_id: string }>("/api/period/match", json(body)),
  run: (id: string) => req<RunStatus>(`/api/runs/${id}`),
  results: (id: string, status: string, page: number, q: string) =>
    req<{ total: number; rows: ResultRow[] }>(
      `/api/runs/${id}/results?status=${status}&page=${page}&size=50&q=${encodeURIComponent(q)}`,
    ),
  decide: (id: string, decisions: { bank_row_id: string; inhouse_id: string | null }[]) =>
    req<{ saved: number; summary: Record<string, number>; shared: boolean | null }>(`/api/runs/${id}/decisions`, json(decisions)),
  searchInhouse: (q: string) => req<InhouseHit[]>(`/api/inhouse/search?q=${encodeURIComponent(q)}`),
  aliases: () => req<Record<string, string>[]>("/api/aliases"),
  history: () => req<Record<string, string>[]>("/api/history"),
  exportUrl: (id: string) => `/api/runs/${id}/export`,
  masterExportUrl: (id: string) => `/api/runs/${id}/master`,
};

/** Poll a background job until it finishes; `onTick` sees each status. Rejects with the job's error. */
export async function waitForJob<T>(id: string, onTick?: (j: Job<T>) => void): Promise<T> {
  for (;;) {
    const j = await api.job<T>(id);
    onTick?.(j);
    if (j.done) {
      if (j.error) throw new Error(j.error);
      return j.result as T;
    }
    await new Promise((r) => setTimeout(r, 400));
  }
}
