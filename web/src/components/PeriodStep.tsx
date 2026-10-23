import { useEffect, useState } from "react";
import { api, RunStatus, Upload } from "../api";
import { FilePicker } from "./FilePicker";
import { Stat } from "./ReferenceStep";

type Settings = {
  auto_score: number;
  auto_margin: number;
  review_score: number;
  auto_subsidiaries: boolean;
  containment_review: boolean;
  default_country: string;
  flag_possible_subsidiaries: boolean;
  // structural / master tiers
  use_parent_group: boolean;
  parent_majority_share: number;
  parent_min_siblings: number;
  use_parent_name: boolean;
  use_master_contain: boolean;
  use_master_fuzzy: boolean;
  master_fuzzy_auto: number;
  master_fuzzy_review: number;
  master_fuzzy_margin: number;
  use_capiq: boolean;
  conflict_audit: boolean;
};

type PeriodStats = {
  received?: number;
  delivered_rows?: number;
  master_rows_in?: number;
  master_rows?: number;
  master_writebacks?: number;
  conflicts?: number;
  capiq_available?: boolean;
  capiq_hits?: number;
  parent_groups?: number;
  reference_loaded?: boolean;
};

const TIER_LABEL: Record<string, string> = {
  delivered_id: "Prior delivery (same client id)",
  delivered_name: "Prior delivery (same name)",
  master_exact: "Master: exact name",
  capiq: "Capital IQ id → universe",
  parent_unique: "Parent group: all siblings agree",
  parent_name: "Parent name = Coalition name",
  master_contain: "Master: name containment",
  master_fuzzy: "Master: close spelling",
  parent_majority: "Parent group: majority (review)",
  parent_thin: "Parent group: too few siblings (review)",
  leftover: "No reference loaded",
};

function tierLabel(t: string) {
  if (TIER_LABEL[t]) return TIER_LABEL[t];
  if (t.startsWith("universe:")) return `Universe / GLEIF (${t.slice(9)})`;
  return t;
}

export function PeriodStep({
  run,
  onRun,
  onReview,
}: {
  run: RunStatus | null;
  onRun: (r: RunStatus) => void;
  onReview: () => void;
}) {
  const [master, setMaster] = useState<Upload | null>(null);
  const [delivered, setDelivered] = useState<Upload | null>(null);
  const [received, setReceived] = useState<Upload | null>(null);
  const [capiq, setCapiq] = useState<Upload | null>(null);
  const [showCapiq, setShowCapiq] = useState(false);
  const [masterMap, setMasterMap] = useState<Record<string, string | null>>({});
  const [deliveredMap, setDeliveredMap] = useState<Record<string, string | null>>({});
  const [receivedMap, setReceivedMap] = useState<Record<string, string | null>>({});
  const [capiqMap, setCapiqMap] = useState<Record<string, string | null>>({});
  const [bankCode, setBankCode] = useState("PERIOD");
  const [s, setS] = useState<Settings>({
    auto_score: 0.92,
    auto_margin: 0.06,
    review_score: 0.8,
    auto_subsidiaries: true,
    containment_review: true,
    default_country: "",
    flag_possible_subsidiaries: true,
    use_parent_group: true,
    parent_majority_share: 0.8,
    parent_min_siblings: 2,
    use_parent_name: true,
    use_master_contain: true,
    use_master_fuzzy: true,
    master_fuzzy_auto: 95,
    master_fuzzy_review: 90,
    master_fuzzy_margin: 5,
    use_capiq: true,
    conflict_audit: true,
  });
  const [err, setErr] = useState("");
  const running = run && (run.stage === "loading" || run.stage === "matching");
  const ready = !!(master && delivered && received && receivedMap.name);

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
    if (!master || !delivered || !received) return;
    setErr("");
    try {
      const { run_id } = await api.startPeriod({
        master_file_id: master.file_id,
        delivered_file_id: delivered.file_id,
        received_file_id: received.file_id,
        master_mapping: masterMap,
        delivered_mapping: deliveredMap,
        received_mapping: receivedMap,
        capiq_file_id: capiq?.file_id ?? null,
        capiq_mapping: capiqMap,
        bank_code: bankCode,
        ...s,
      });
      onRun({ run_id, bank_code: bankCode.toUpperCase(), stage: "loading", progress: 0.05 });
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  const sum = run?.summary ?? {};
  const period = (run as RunStatus & { period?: PeriodStats })?.period;
  const tiers = run?.by_tier ?? {};

  return (
    <div className="stack">
      <section className="card">
        <h3>Period mapping</h3>
        <p className="hint">
          Cascade: prior delivery → manual master → bank group structure → universe / GLEIF for the rest.
          New Coalition hits are written back to the master with a timestamp and the tier that found them.
        </p>
        <ol className="hint" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
          <li>Carry forward Mapped rows from the last delivery (by client id, then name).</li>
          <li>Exact match against the master Granular Name list; Capital IQ id if the bank sends one.</li>
          <li>Parent group: if every Mapped sibling under the bank's parent went to one Coalition, inherit it.</li>
          <li>Master near-misses: name containment and close spellings (high precision only).</li>
          <li>Remainder: published universe (GLEIF subsidiaries / names); parent majority offered for review.</li>
          <li>Conflict audit: any auto that disagrees with the parent group, parent name or CapIQ goes to review.</li>
        </ol>
        <label className="field narrow">
          <span>Bank / book code</span>
          <input
            value={bankCode}
            onChange={(e) => setBankCode(e.target.value.toUpperCase())}
            placeholder="e.g. BARC"
            aria-label="Bank code"
          />
        </label>
      </section>

      <FilePicker
        kind="period_master"
        title="1. Master list"
        hint="Manual Coalition list (Granular Name → Coalition ID). Incomplete is fine — it grows as we map."
        required={["name"]}
        upload={master}
        mapping={masterMap}
        onUpload={setMaster}
        onMapping={setMasterMap}
      />
      <FilePicker
        kind="period_delivered"
        title="2. Prior delivery"
        hint="What you sent the bank last time (Status + Coalition ID)."
        required={["name"]}
        upload={delivered}
        mapping={deliveredMap}
        onUpload={setDelivered}
        onMapping={setDeliveredMap}
      />
      <FilePicker
        kind="period_received"
        title="3. Received this period"
        hint="This period's bank file (client_leid / client_lename)."
        required={["name"]}
        upload={received}
        mapping={receivedMap}
        onUpload={setReceived}
        onMapping={setReceivedMap}
      />

      {showCapiq ? (
        <FilePicker
          kind="period_capiq"
          title="4. Capital IQ bridge (optional)"
          hint="Capital IQ id → Coalition ID. Used when the bank's client id is a CapIQ id. The universe's own CapIQ ids are always used; this adds more."
          required={["ciq_id", "inhouse_id"]}
          upload={capiq}
          mapping={capiqMap}
          onUpload={setCapiq}
          onMapping={setCapiqMap}
        />
      ) : (
        <p className="hint">
          <button className="link" onClick={() => setShowCapiq(true)}>
            Add a Capital IQ id → Coalition ID file (optional)
          </button>
        </p>
      )}

      <section className="card">
        <h3>Structure and master near-misses</h3>
        <p className="hint">These run before the universe and are the main lift on subsidiaries with unfamiliar names.</p>
        <label className="check">
          <input type="checkbox" checked={s.use_parent_group} onChange={(e) => setS({ ...s, use_parent_group: e.target.checked })} />
          Parent group inheritance (all Mapped siblings agree → auto; majority or too few siblings → review candidate)
        </label>
        {s.use_parent_group && (
          <div className="sliders">
            <Slider label="Majority: share of siblings agreeing" min={0.5} max={1} value={s.parent_majority_share} onChange={(v) => setS({ ...s, parent_majority_share: v })} />
            <label className="slider">
              <span>
                Minimum Mapped siblings to trust a group: <strong>{s.parent_min_siblings}</strong>
              </span>
              <input type="range" min={1} max={10} step={1} value={s.parent_min_siblings} onChange={(e) => setS({ ...s, parent_min_siblings: Number(e.target.value) })} />
            </label>
          </div>
        )}
        <label className="check">
          <input type="checkbox" checked={s.use_parent_name} onChange={(e) => setS({ ...s, use_parent_name: e.target.checked })} />
          Parent group name equals a Coalition name → auto (when no sibling history)
        </label>
        <label className="check">
          <input type="checkbox" checked={s.use_master_contain} onChange={(e) => setS({ ...s, use_master_contain: e.target.checked })} />
          Master name containment (one Coalition only; 2 shared words need the parent group to agree)
        </label>
        <label className="check">
          <input type="checkbox" checked={s.use_master_fuzzy} onChange={(e) => setS({ ...s, use_master_fuzzy: e.target.checked })} />
          Master close spellings (token-sorted similarity)
        </label>
        {s.use_master_fuzzy && (
          <div className="sliders">
            <Slider label="Auto at similarity ≥" min={85} max={100} step={1} value={s.master_fuzzy_auto} onChange={(v) => setS({ ...s, master_fuzzy_auto: v })} />
            <Slider label="Review at similarity ≥" min={80} max={99} step={1} value={s.master_fuzzy_review} onChange={(v) => setS({ ...s, master_fuzzy_review: v })} />
            <Slider label="Auto: minimum gap to a different Coalition" min={0} max={20} step={1} value={s.master_fuzzy_margin} onChange={(v) => setS({ ...s, master_fuzzy_margin: v })} />
          </div>
        )}
        <label className="check">
          <input type="checkbox" checked={s.use_capiq} onChange={(e) => setS({ ...s, use_capiq: e.target.checked })} />
          Capital IQ id bridge (silent when the bank's ids are not CapIQ ids)
        </label>
        <label className="check">
          <input type="checkbox" checked={s.conflict_audit} onChange={(e) => setS({ ...s, conflict_audit: e.target.checked })} />
          Conflict audit: downgrade autos that disagree with parent group / parent name / CapIQ
        </label>
      </section>

      <section className="card">
        <h3>Universe / GLEIF thresholds</h3>
        <p className="hint">Used only for rows not settled by the tiers above.</p>
        <div className="sliders">
          <Slider label="Auto-match: minimum score" min={0.8} max={1} value={s.auto_score} onChange={(v) => setS({ ...s, auto_score: v })} />
          <Slider label="Auto-match: minimum gap to runner-up" min={0} max={0.2} value={s.auto_margin} onChange={(v) => setS({ ...s, auto_margin: v })} />
          <Slider label="Review: minimum score (below = no match)" min={0.6} max={0.95} value={s.review_score} onChange={(v) => setS({ ...s, review_score: v })} />
        </div>
        <label className="check">
          <input type="checkbox" checked={s.auto_subsidiaries} onChange={(e) => setS({ ...s, auto_subsidiaries: e.target.checked })} />
          Auto-accept GLEIF-confirmed subsidiaries
        </label>
        <label className="check">
          <input type="checkbox" checked={s.containment_review} onChange={(e) => setS({ ...s, containment_review: e.target.checked })} />
          Always review name-containment hits
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={s.flag_possible_subsidiaries}
            onChange={(e) => setS({ ...s, flag_possible_subsidiaries: e.target.checked })}
          />
          Flag possible subsidiaries for review
        </label>
      </section>

      <div className="actions">
        <button className="primary" disabled={!ready || !!running} onClick={start}>
          {running ? `Running… ${Math.round((run?.progress ?? 0) * 100)}%` : "Run period map"}
        </button>
        {err && <p className="error">{err}</p>}
      </div>

      {run?.stage === "error" && <p className="error">{run.error}</p>}

      {run?.stage === "done" && (
        <section className="card ok">
          <h3>Results</h3>
          <div className="stats">
            <Stat label="Auto-mapped" value={sum.auto ?? 0} />
            <Stat label="Review" value={sum.review ?? 0} />
            <Stat label="No match" value={sum.no_match ?? 0} />
            <Stat label="Conflicts flagged" value={period?.conflicts ?? 0} />
            <Stat label="Master write-backs" value={period?.master_writebacks ?? 0} />
          </div>
          {period && (
            <p className="hint">
              Received {period.received?.toLocaleString()} · prior delivery {period.delivered_rows?.toLocaleString()} ·{" "}
              {period.parent_groups?.toLocaleString()} bank parent groups with history · master in{" "}
              {period.master_rows_in?.toLocaleString()} → out {period.master_rows?.toLocaleString()}
              {period.reference_loaded ? " · universe/GLEIF loaded" : " · no universe reference (exact tiers only)"}
              {period.capiq_available
                ? ` · CapIQ bridge: ${period.capiq_hits ?? 0} hit${period.capiq_hits === 1 ? "" : "s"}`
                : " · CapIQ bridge: no ids available"}
              {run.seconds != null ? ` · ${run.seconds}s` : ""}
            </p>
          )}
          {Object.keys(tiers).length > 0 && (
            <div className="table-wrap">
              <table className="tiers">
                <thead>
                  <tr>
                    <th>Tier</th>
                    <th>Auto</th>
                    <th>Review</th>
                    <th>No match</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(tiers).map(([t, counts]) => (
                    <tr key={t}>
                      <td title={t}>{tierLabel(t)}</td>
                      <td>{counts.auto ?? 0}</td>
                      <td>{counts.review ?? 0}</td>
                      <td>{counts.no_match ?? 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="actions" style={{ marginTop: 12 }}>
            <button className="primary" onClick={onReview}>
              Review leftovers
            </button>
            <a className="button" href={api.exportUrl(run.run_id)}>
              Download delivery workbook
            </a>
            <a className="button" href={api.masterExportUrl(run.run_id)}>
              Download updated master
            </a>
          </div>
        </section>
      )}
    </div>
  );
}

function Slider({
  label,
  min,
  max,
  step = 0.01,
  value,
  onChange,
}: {
  label: string;
  min: number;
  max: number;
  step?: number;
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="slider">
      <span>
        {label}: <strong>{step >= 1 ? value.toFixed(0) : value.toFixed(2)}</strong>
      </span>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
    </label>
  );
}
