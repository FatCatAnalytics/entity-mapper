import { useEffect, useRef, useState } from "react";
import { api, Upload, Validation } from "../api";

const LABELS: Record<string, string> = {
  inhouse_id: "Coalition / in-house ID",
  bank_row_id: "Bank row ID",
  name: "Entity name",
  country: "Country",
  city: "City",
  lei: "LEI",
  ciq_id: "Client LE ID / CapIQ",
  bank_code: "Bank code",
  status: "Status",
  coalition_names: "Coalition Names",
  mapped_at: "Mapped At",
  source: "Source",
};

type Props = {
  kind: "inhouse" | "bank" | "master" | "period_master" | "period_delivered" | "period_received" | "period_capiq";
  title: string;
  required: string[];
  upload: Upload | null;
  mapping: Record<string, string | null>;
  onUpload: (u: Upload) => void;
  onMapping: (m: Record<string, string | null>) => void;
  hint?: string;
};

export function FilePicker({ kind, title, required, upload, mapping, onUpload, onMapping, hint }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [drag, setDrag] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [pasted, setPasted] = useState("");
  const [hasHeader, setHasHeader] = useState(true);
  const [check, setCheck] = useState<Validation | null>(null);

  // Bank / received files are checked as soon as the columns are mapped.
  const mapKey = JSON.stringify(mapping);
  useEffect(() => {
    if ((kind !== "bank" && kind !== "period_received") || !upload) {
      setCheck(null);
      return;
    }
    let live = true;
    api.validate(upload.file_id, mapping).then((v) => live && setCheck(v)).catch(() => live && setCheck(null));
    return () => {
      live = false;
    };
  }, [kind, upload?.file_id, mapKey]);

  async function handle(file?: File, header = true) {
    if (!file) return;
    setBusy(true);
    setErr("");
    try {
      const u = await api.upload(file, kind, header);
      onUpload(u);
      onMapping(u.suggested);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h3>{title}</h3>
      {hint && <p className="hint">{hint}</p>}
      <div
        className={`drop ${drag ? "drag" : ""}`}
        onClick={() => input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          handle(e.dataTransfer.files[0]);
        }}
      >
        <input
          ref={input}
          type="file"
          accept=".csv,.txt,.xlsx,.xls"
          hidden
          data-testid={`file-${kind}`}
          onChange={(e) => handle(e.target.files?.[0])}
        />
        {busy ? (
          <span>Reading file…</span>
        ) : upload ? (
          <span>
            <strong>{upload.filename}</strong> · {upload.rows.toLocaleString()} rows · click to replace
          </span>
        ) : (
          <span>Drop a CSV or Excel file here, or click to choose</span>
        )}
      </div>
      {kind === "bank" && (
        <p className="hint">
          <button className="link" onClick={() => setPasting(!pasting)}>
            {pasting ? "Hide the paste box" : "Or paste rows from a spreadsheet"}
          </button>
        </p>
      )}
      {pasting && (
        <div className="paste">
          <textarea
            value={pasted}
            onChange={(e) => setPasted(e.target.value)}
            rows={6}
            spellCheck={false}
            aria-label="Pasted rows"
            placeholder={"Copy the cells in Excel and paste here, e.g.\nClient name\tCountry\tLEI\nAcme Holdings PLC\tGB\t"}
          />
          <div className="actions">
            <button
              className="primary"
              disabled={!pasted.trim() || busy}
              onClick={() => handle(new File([pasted], "pasted.tsv", { type: "text/tab-separated-values" }), hasHeader)}
            >
              Use these rows
            </button>
            <label className="check" style={{ marginTop: 0 }}>
              <input type="checkbox" checked={hasHeader} onChange={(e) => setHasHeader(e.target.checked)} />
              The first row is the column headings
            </label>
            <span className="hint">Best for small lists. For a full submission, drop the file: nothing is retyped.</span>
          </div>
        </div>
      )}
      {err && <p className="error">{err}</p>}
      {upload && check && (
        <div className={`issues ${check.ok ? "" : "bad"}`} data-testid="validation">
          {check.issues.length === 0 ? (
            <p className="okmsg">Checked {check.rows.toLocaleString()} rows: no problems found.</p>
          ) : (
            <>
              <p className="hint" style={{ marginTop: 0 }}>
                Checked {check.rows.toLocaleString()} rows. {check.ok ? "You can match as it is." : "Fix the red items, or match anyway: those rows will not match."}
              </p>
              <ul>
                {check.issues.map((i) => (
                  <li key={i.text} className={`lvl-${i.level}`}>
                    <strong>{i.count.toLocaleString()} {i.count === 1 ? "row" : "rows"}</strong> {i.text}
                    {i.examples.length > 0 && <span className="hint"> · {i.examples.join("; ")}</span>}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
      {upload && (
        <>
          <p className="hint">Map your columns. Fields marked * are required.</p>
          <div className="mapping">
            {upload.fields.map((f) => (
              <label key={f}>
                <span>
                  {kind === "master" && f === "name" ? "Bank's entity name" : LABELS[f] ?? f}
                  {required.includes(f) ? " *" : ""}
                </span>
                <select
                  value={mapping[f] ?? ""}
                  onChange={(e) => onMapping({ ...mapping, [f]: e.target.value || null })}
                >
                  <option value="">(none)</option>
                  {upload.columns.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
          <details>
            <summary>Preview first rows</summary>
            <div className="table-wrap">
              <table className="preview">
                <thead>
                  <tr>{upload.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {upload.preview.map((r, i) => (
                    <tr key={i}>{upload.columns.map((c) => <td key={c}>{r[c]}</td>)}</tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </section>
  );
}
