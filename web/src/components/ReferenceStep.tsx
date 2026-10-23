import { useEffect, useState } from "react";
import { api, MasterSummary, RefInfo, Upload } from "../api";
import { FilePicker } from "./FilePicker";

export function ReferenceStep({ info, onBuilt }: { info: RefInfo | null; onBuilt: (r: RefInfo) => void }) {
  const [upload, setUpload] = useState<Upload | null>(null);
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [l1, setL1] = useState("");
  const [rr, setRr] = useState("");
  const [ciq, setCiq] = useState<Upload | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [mUpload, setMUpload] = useState<Upload | null>(null);
  const [mMapping, setMMapping] = useState<Record<string, string | null>>({});
  const [mReplace, setMReplace] = useState(false);
  const [mSum, setMSum] = useState<MasterSummary | null>(null);
  const [mErr, setMErr] = useState("");
  const [uni, setUni] = useState<Upload | null>(null);
  const [uniErr, setUniErr] = useState("");

  useEffect(() => {
    api.master().then(setMSum).catch(() => undefined);
  }, []);

  async function importMaster() {
    if (!mUpload) return;
    setMErr("");
    try {
      setMSum(await api.importMaster({ file_id: mUpload.file_id, mapping: mMapping, replace: mReplace }));
    } catch (e) {
      setMErr((e as Error).message);
    }
  }

  async function build() {
    if (!upload && !uni) return;
    setBusy(true);
    setErr("");
    try {
      const r = uni ? await api.buildReference({ universe_file_id: uni.file_id, mapping: {} }) : await api.buildReference({
        inhouse_file_id: upload!.file_id,
        mapping,
        gleif_l1_path: l1.trim() || undefined,
        gleif_rr_path: rr.trim() || undefined,
        ciq_file_id: ciq?.file_id,
      });
      onBuilt(r);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <section className="card">
        <h3>Load a universe master file by hand</h3>
        <p className="hint" style={{ marginTop: 0 }}>
          Normally not needed: the Start tab loads the version the owner published. Use this to try a file before it
          is published, or to work without the shared folder. It applies to this session only.
        </p>
        <input
          type="file"
          accept=".xlsx"
          data-testid="file-universe"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            if (!f) return;
            setUniErr("");
            try {
              setUni(await api.upload(f, "universe"));
            } catch (x) {
              setUniErr((x as Error).message);
            }
          }}
        />
        {uni && (
          <p className="hint">
            <strong>{uni.filename}</strong> ·{" "}
            {Object.entries(uni.roles ?? {})
              .map(([k, v]) => `${v.toLocaleString()} ${k === "group" ? "entities" : k === "subsidiary" ? "subsidiaries" : "successor LEIs"}`)
              .join(" · ")}{" "}
            · <button className="link" onClick={() => setUni(null)}>use a plain list instead</button>
          </p>
        )}
        {uniErr && <p className="error">{uniErr}</p>}
      </section>

      {!uni && (
      <>
      <FilePicker
        kind="inhouse"
        title="Or: in-house entity list (with optional GLEIF files below)"
        required={["inhouse_id", "name"]}
        upload={upload}
        mapping={mapping}
        onUpload={setUpload}
        onMapping={setMapping}
      />

      </>
      )}

      <FilePicker
        kind="master"
        title="Master mapping (previous mappings, optional)"
        required={["name", "inhouse_id"]}
        upload={mUpload}
        mapping={mMapping}
        onUpload={setMUpload}
        onMapping={setMMapping}
      />
      <section className="card">
        <p className="hint" style={{ marginTop: 0 }}>
          Checked first on every run: a bank name already mapped by a person goes straight to that in-house entity, after
          country and legal-form checks. The spellings also help fuzzy matching find variants. Reviewer decisions are
          added automatically.
        </p>
        <div className="actions">
          <button className="primary" disabled={!mUpload} onClick={importMaster}>
            Import mapping
          </button>
          <label className="check" style={{ marginTop: 0 }}>
            <input type="checkbox" checked={mReplace} onChange={(e) => setMReplace(e.target.checked)} />
            Replace the existing imported mapping
          </label>
          {mErr && <span className="error">{mErr}</span>}
        </div>
        {mSum && (
          <p className="hint">
            {mSum.imported != null && <strong>Imported {mSum.imported.toLocaleString()} rows. </strong>}
            In store: {mSum.imported_rows.toLocaleString()} imported mappings, {mSum.reviewer_decisions.toLocaleString()}{" "}
            reviewer decisions.
            {mSum.unknown_inhouse_ids ? ` ${mSum.unknown_inhouse_ids.toLocaleString()} point to in-house IDs no longer in the list and are ignored.` : ""}
            {mSum.note ? ` ${mSum.note}` : ""}
            {mSum.shared_note ? ` ${mSum.shared_note}` : ""}
          </p>
        )}
      </section>

      {!uni && (
      <>
      <section className="card">
        <h3>Optional bridges</h3>
        <div className="grid2">
          <div>
            <h4>GLEIF Golden Copy (local files)</h4>
            <p className="hint">
              Paths on this computer to the Golden Copy files from gleif.org (the .csv.zip files work as downloaded).
              Only records linked to your LEIs are kept; bank names are never sent to GLEIF. First build takes a few
              minutes, later builds are instant.
            </p>
            <label className="field">
              <span>Level 1 LEI-CDF CSV, or a simple extract</span>
              <input value={l1} onChange={(e) => setL1(e.target.value)} placeholder="~/Data/gleif/...-lei2-golden-copy.csv.zip" />
            </label>
            <label className="field">
              <span>Level 2 relationships CSV (optional)</span>
              <input value={rr} onChange={(e) => setRr(e.target.value)} placeholder="~/Data/gleif/...-rr-golden-copy.csv.zip" />
            </label>
          </div>
          <div>
            <h4>Capital IQ export</h4>
            <p className="hint">CSV with ciq_id, name, aliases (';'-separated), country.</p>
            <input
              type="file"
              accept=".csv,.xlsx"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) setCiq(await api.upload(f, "ciq"));
              }}
            />
            {ciq && <p className="hint">{ciq.filename} · {ciq.rows.toLocaleString()} rows</p>}
          </div>
        </div>
      </section>

      </>
      )}

      <div className="actions">
        <button className="primary" disabled={(!upload && !uni) || busy} onClick={build}>
          {busy ? "Building index…" : "Build reference index"}
        </button>
        {err && <span className="error">{err}</span>}
      </div>

      {info && (
        <section className="card ok">
          <h3>Reference ready{info.source === "universe master" ? ` · from ${info.universe_file}` : ""}</h3>
          <div className="stats">
            <Stat label="In-house entities" value={info.inhouse_entities} />
            <Stat label="Name variants" value={info.name_variants} />
            <Stat label="LEIs indexed" value={info.leis_indexed} />
            <Stat label="CIQ IDs indexed" value={info.ciq_ids_indexed} />
          </div>
          <p className="hint">
            Previous mappings used: {(info.master_rows ?? 0).toLocaleString()} · GLEIF name-country check:{" "}
            {info.gleif_name_index ? "on" : "off (add the Golden Copy to enable)"}
            {info.gleif_subsidiaries ? ` · ${info.gleif_subsidiaries.toLocaleString()} GLEIF subsidiaries linked to your groups` : ""}
          </p>
          {info.lei_issues && (
            <p className="hint">
              LEI check against GLEIF: <strong>{(info.lei_issues.action ?? 0).toLocaleString()} to fix</strong> (retired,
              merged, annulled, not found, or on two rows) · {(info.lei_issues.check ?? 0).toLocaleString()} worth a look
              (renamed or possibly the wrong LEI)
              {info.lei_issues.info ? ` · ${info.lei_issues.info.toLocaleString()} lapsed (fine for matching)` : ""}. <a href="/api/reference/lei-check">Download the LEI check</a>
            </p>
          )}
          <p className="hint">
            Variants by source:{" "}
            {Object.entries(info.variants_by_source)
              .map(([k, v]) => `${k} ${v.toLocaleString()}`)
              .join(" · ")}{" "}
            · built in {info.built_in_s}s
          </p>
        </section>
      )}
    </div>
  );
}

export function Stat({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return (
    <div className={`stat ${tone ?? ""}`}>
      <div className="stat-value">{typeof value === "number" ? value.toLocaleString() : value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
