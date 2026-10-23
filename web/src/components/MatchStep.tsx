import { useEffect, useState } from "react";
import { api, RunStatus, Upload } from "../api";
import { FilePicker } from "./FilePicker";
import { Stat } from "./ReferenceStep";

type Settings = { auto_score: number; auto_margin: number; review_score: number; auto_subsidiaries: boolean; containment_review: boolean; default_country: string; flag_possible_subsidiaries: boolean };

export function MatchStep({
  ready,
  run,
  onRun,
  onReview,
}: {
  ready: boolean;
  run: RunStatus | null;
  onRun: (r: RunStatus) => void;
  onReview: () => void;
}) {
  const [upload, setUpload] = useState<Upload | null>(null);
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [bankCode, setBankCode] = useState("");
  const [s, setS] = useState<Settings>({ auto_score: 0.92, auto_margin: 0.06, review_score: 0.8, auto_subsidiaries: true, containment_review: true, default_country: "", flag_possible_subsidiaries: true });
  const [err, setErr] = useState("");
  const running = run && (run.stage === "loading" || run.stage === "matching");

  useEffect(() => {
    if (!run || !running) return;
    const t = setInterval(async () => {
      try {
        onRun(await api.run(run.run_id));
      } catch (e) {
        setErr((e as Error).message);
      }
    }, 700);
    return () => clearInterval(t);
  }, [run?.run_id, running]);

  async function start() {
    if (!upload) return;
    setErr("");
    try {
      const { run_id } = await api.startMatch({ bank_file_id: upload.file_id, mapping, bank_code: bankCode, ...s });
      onRun({ run_id, bank_code: bankCode.toUpperCase(), stage: "loading", progress: 0.05 });
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  if (!ready) return <p className="empty">Build the reference index first.</p>;

  const sum = run?.summary ?? {};
  return (
    <div className="stack">
      <section className="card">
        <h3>Submitting bank</h3>
        <label className="field narrow">
          <span>Bank code *</span>
          <input
            value={bankCode}
            onChange={(e) => setBankCode(e.target.value.toUpperCase())}
            placeholder="e.g. BARC"
            aria-label="Bank code"
          />
        </label>
        <p className="hint">Reviewer decisions are remembered per bank code and reused on its next file.</p>
        <label className="field narrow">
          <span>Default country for rows without one (optional)</span>
          <input
            value={s.default_country}
            onChange={(e) => setS({ ...s, default_country: e.target.value })}
            placeholder="e.g. GB for a domestic book"
            aria-label="Default country"
          />
        </label>
        <p className="hint">
          Leave blank if the file mixes countries. Name-only files work, but a country (or this default) is the best
          defence against same-named companies in different countries.
        </p>
      </section>

      <FilePicker
        kind="bank"
        title="Bank submission"
        required={["name"]}
        upload={upload}
        mapping={mapping}
        onUpload={setUpload}
        onMapping={setMapping}
      />

      <section className="card">
        <h3>Thresholds</h3>
        <div className="sliders">
          <Slider label="Auto-match: minimum score" min={0.8} max={1} value={s.auto_score} onChange={(v) => setS({ ...s, auto_score: v })} />
          <Slider label="Auto-match: minimum gap to runner-up" min={0} max={0.2} value={s.auto_margin} onChange={(v) => setS({ ...s, auto_margin: v })} />
          <Slider label="Review: minimum score (below = no match)" min={0.6} max={0.95} value={s.review_score} onChange={(v) => setS({ ...s, review_score: v })} />
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={s.auto_subsidiaries}
            onChange={(e) => setS({ ...s, auto_subsidiaries: e.target.checked })}
          />
          Auto-match subsidiaries GLEIF confirms (by LEI or registered name) to your group record. Name-only guesses always go to review
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={s.containment_review}
            onChange={(e) => setS({ ...s, containment_review: e.target.checked })}
          />
          Send to review when every bank word appears in a longer in-house name ("Apple Bank" in "Apple Bank for Savings")
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={s.flag_possible_subsidiaries}
            onChange={(e) => setS({ ...s, flag_possible_subsidiaries: e.target.checked })}
          />
          Flag possible subsidiaries: a bank name starting with one of your group names ("3M United Kingdom PLC") goes to
          review against that group
        </label>
      </section>

      <div className="actions">
        <button className="primary" disabled={!upload || !bankCode || !!running} onClick={start}>
          {running ? "Matching…" : "Run matching"}
        </button>
        {err && <span className="error">{err}</span>}
      </div>

      {run && (
        <section className={`card ${run.stage === "done" ? "ok" : ""}`}>
          {running && (
            <>
              <h3>{run.stage === "loading" ? "Reading file…" : "Matching…"}</h3>
              <div className="bar">
                <div style={{ width: `${Math.round(run.progress * 100)}%` }} />
              </div>
            </>
          )}
          {run.stage === "error" && <p className="error">{run.error}</p>}
          {run.stage === "done" && (
            <>
              <h3>
                {run.bank_code}: matched in {run.seconds}s
              </h3>
              <div className="stats">
                <Stat label="Auto-matched" value={sum.auto ?? 0} tone="good" />
                <Stat label="Needs review" value={sum.review ?? 0} tone="warn" />
                <Stat label="No match" value={sum.no_match ?? 0} />
                <Stat label="Decided by reviewer" value={sum.manual ?? 0} />
              </div>
              {run.profile && <Profile p={run.profile} master={run.master} />}
              <TierTable byTier={run.by_tier ?? {}} />
              <div className="actions">
                <button className="primary" onClick={onReview}>
                  Review {(sum.review ?? 0).toLocaleString()} rows
                </button>
                <a className="button" href={api.exportUrl(run.run_id)}>
                  Download Excel
                </a>
              </div>
            </>
          )}
        </section>
      )}
    </div>
  );
}

function Slider({ label, min, max, value, onChange }: { label: string; min: number; max: number; value: number; onChange: (v: number) => void }) {
  return (
    <label className="slider">
      <span>
        {label} <strong>{value.toFixed(2)}</strong>
      </span>
      <input type="range" min={min} max={max} step={0.01} value={value} onChange={(e) => onChange(parseFloat(e.target.value))} />
    </label>
  );
}

function TierTable({ byTier }: { byTier: Record<string, Record<string, number>> }) {
  const tiers = Object.keys(byTier);
  const cols = ["auto", "review", "no_match"];
  const names: Record<string, string> = { master: "Previous mapping", alias: "This bank's decisions", id: "LEI / CIQ ID", exact: "Exact name", fuzzy: "Fuzzy", none: "Empty name" };
  return (
    <table className="tiers">
      <thead>
        <tr>
          <th>Tier</th>
          {cols.map((c) => <th key={c}>{c.replace("_", " ")}</th>)}
        </tr>
      </thead>
      <tbody>
        {tiers.map((t) => (
          <tr key={t}>
            <td>{names[t] ?? t}</td>
            {cols.map((c) => <td key={c}>{(byTier[t]?.[c] ?? 0).toLocaleString()}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const FIELD_NAMES: Record<string, string> = { name: "Name", country: "Country", city: "City", lei: "LEI", ciq_id: "CIQ ID" };

function Profile({ p, master }: { p: NonNullable<RunStatus["profile"]>; master?: RunStatus["master"] }) {
  return (
    <div className="profile">
      <div className="chips">
        <span className="hint">This file contains:</span>
        {Object.entries(p.fill).map(([k, v]) => (
          <span key={k} className={`chip ${v >= 0.5 ? "on" : "off"}`}>
            {FIELD_NAMES[k] ?? k} {Math.round(v * 100)}%
          </span>
        ))}
        {master && master.used > 0 && <span className="chip on">Previous mappings {master.used.toLocaleString()}</span>}
      </div>
      {p.notes.map((n) => (
        <p key={n} className="hint">{n}</p>
      ))}
    </div>
  );
}
