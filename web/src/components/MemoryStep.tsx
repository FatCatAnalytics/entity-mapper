import { useEffect, useState } from "react";
import { api, Conflict, Startup } from "../api";

const label = (id: string) => (id === "__NO_MATCH__" ? "no match" : id);

export function MemoryStep({ status, onStatus }: { status: Startup | null; onStatus: (s: Startup) => void }) {
  const [err, setErr] = useState("");
  const conflicts = status?.sync?.conflicts ?? [];
  const isOwner = !!status?.shared.is_owner;

  async function settle(c: Conflict, inhouseId: string) {
    setErr("");
    try {
      await api.resolve(c, inhouseId === "__NO_MATCH__" ? "" : inhouseId);
      onStatus(await api.startup());
      api.aliases().then(setAliases);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  const [aliases, setAliases] = useState<Record<string, string>[]>([]);
  const [runs, setRuns] = useState<Record<string, string>[]>([]);
  useEffect(() => {
    api.aliases().then(setAliases);
    api.history().then(setRuns);
  }, []);
  return (
    <div className="stack">
      {conflicts.length > 0 && (
        <section className="card warn">
          <h3>Conflicting decisions ({conflicts.length.toLocaleString()})</h3>
          <p className="hint" style={{ marginTop: 0 }}>
            Two people mapped the same bank name differently. The latest decision is used until the owner
            {status?.shared.owner ? ` (${status.shared.owner})` : ""} settles it; the owner's choice then applies to everyone.
          </p>
          {err && <p className="error">{err}</p>}
          <div className="table-wrap">
            <table className="preview">
              <thead>
                <tr><th>Bank</th><th>Name</th><th>Country</th><th>Decisions</th></tr>
              </thead>
              <tbody>
                {conflicts.map((c) => (
                  <tr key={`${c.bank_code}|${c.name_clean}|${c.country}`}>
                    <td>{c.bank_code}</td>
                    <td>{c.raw_name}</td>
                    <td>{c.country}</td>
                    <td>
                      {c.options.map((o) => (
                        <span key={o.decided_by} className="conflict-option">
                          <strong>{label(o.inhouse_id)}</strong> by {o.decided_by}, {o.decided_at.slice(0, 10)}
                          {o.inhouse_id === c.current ? " (in use)" : ""}
                          {isOwner && (
                            <button className="link" onClick={() => settle(c, o.inhouse_id)}>use this</button>
                          )}
                        </span>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <section className="card">
        <h3>Alias memory</h3>
        <p className="hint">Confirmed decisions, reused automatically for the same bank, cleaned name and country.</p>
        <Table rows={aliases} cols={["bank_code", "raw_name", "country", "inhouse_id", "decided_by", "decided_at"]} />
      </section>
      <section className="card">
        <h3>Run history</h3>
        <Table rows={runs} cols={["bank_code", "started_at", "n_rows", "n_auto", "n_review", "n_no_match", "run_by"]} />
      </section>
    </div>
  );
}

function Table({ rows, cols }: { rows: Record<string, string>[]; cols: string[] }) {
  if (!rows.length) return <p className="empty">Nothing yet.</p>;
  return (
    <div className="table-wrap">
      <table className="preview">
        <thead>
          <tr>{cols.map((c) => <th key={c}>{c.replace(/_/g, " ")}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c}>{c === "inhouse_id" && r[c] === "__NO_MATCH__" ? "no match" : r[c]}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
