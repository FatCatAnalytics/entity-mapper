import { useEffect, useState } from "react";
import { api, InhouseHit, ResultRow, RunStatus } from "../api";

type Pending = Record<string, { inhouse_id: string | null; label: string }>;

export function ReviewStep({ run, onSummary }: { run: RunStatus | null; onSummary: (s: Record<string, number>) => void }) {
  const [status, setStatus] = useState("review");
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [data, setData] = useState<{ total: number; rows: ResultRow[] }>({ total: 0, rows: [] });
  const [pending, setPending] = useState<Pending>({});
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  async function load() {
    if (!run || run.stage !== "done") return;
    try {
      setData(await api.results(run.run_id, status, page, q));
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  useEffect(() => {
    load();
  }, [run?.run_id, run?.stage, status, page, q]);

  if (!run || run.stage !== "done") return <p className="empty">Run a match first.</p>;

  const pick = (row: ResultRow, id: string | null, label: string) =>
    setPending((p) => {
      const cur = p[row.bank_row_id];
      const next = { ...p };
      if (cur && cur.inhouse_id === id) delete next[row.bank_row_id];
      else next[row.bank_row_id] = { inhouse_id: id, label };
      return next;
    });

  async function save() {
    setErr("");
    try {
      const decisions = Object.entries(pending).map(([bank_row_id, v]) => ({ bank_row_id, inhouse_id: v.inhouse_id }));
      const r = await api.decide(run!.run_id, decisions);
      setPending({});
      onSummary(r.summary);
      setMsg(
        `Saved ${r.saved} decision(s) to ${run!.bank_code}'s alias memory` +
          (r.shared === true
            ? ", shared with the team."
            : r.shared === false
              ? " on this computer. The shared folder is not reachable; they are sent on the next start."
              : "."),
      );
      load();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  const pages = Math.max(1, Math.ceil(data.total / 50));
  const nPending = Object.keys(pending).length;

  return (
    <div className="stack">
      <div className="toolbar">
        <div className="seg">
          {[
            ["review", "Needs review"],
            ["auto", "Auto-matched"],
            ["no_match", "No match"],
            ["manual", "Decided"],
          ].map(([k, l]) => (
            <button key={k} className={status === k ? "on" : ""} onClick={() => { setStatus(k); setPage(1); }}>
              {l}
            </button>
          ))}
        </div>
        <input className="search" placeholder="Filter by name…" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
        <span className="hint">{data.total.toLocaleString()} rows</span>
        <div className="spacer" />
        <button className="primary" disabled={!nPending} onClick={save}>
          Save {nPending || ""} decision{nPending === 1 ? "" : "s"}
        </button>
      </div>
      {msg && <p className="okmsg">{msg}</p>}
      {err && <p className="error">{err}</p>}

      <div className="rows">
        {data.rows.map((r) => (
          <ReviewRow key={r.bank_row_id} row={r} chosen={pending[r.bank_row_id]} onPick={pick} />
        ))}
        {!data.rows.length && <p className="empty">Nothing here.</p>}
      </div>

      <div className="pager">
        <button disabled={page <= 1} onClick={() => setPage(page - 1)}>‹ Prev</button>
        <span>Page {page} of {pages}</span>
        <button disabled={page >= pages} onClick={() => setPage(page + 1)}>Next ›</button>
      </div>
    </div>
  );
}

function ReviewRow({
  row,
  chosen,
  onPick,
}: {
  row: ResultRow;
  chosen?: { inhouse_id: string | null; label: string };
  onPick: (r: ResultRow, id: string | null, label: string) => void;
}) {
  const [searching, setSearching] = useState(false);
  const [sq, setSq] = useState("");
  const [hits, setHits] = useState<InhouseHit[]>([]);
  const cands = [
    { id: row.inhouse_id, name: row.inhouse_name, score: row.score },
    { id: row.inhouse_id_2, name: row.inhouse_name_2, score: row.score_2 },
    { id: row.inhouse_id_3, name: row.inhouse_name_3, score: row.score_3 },
  ].filter((c) => c.id);

  useEffect(() => {
    if (sq.trim().length < 2) return setHits([]);
    const t = setTimeout(async () => setHits(await api.searchInhouse(sq)), 250);
    return () => clearTimeout(t);
  }, [sq]);

  return (
    <div className={`row ${chosen ? "chosen" : ""}`}>
      <div className="bank">
        <div className="name">{row.bank_name}</div>
        <div className="meta">
          {row.bank_country || "no country"} · row {row.bank_row_id} · tier {row.tier}
          {row.relation === "subsidiary" && <span className="tag">GLEIF subsidiary</span>}
        </div>
        <div className="evidence">{row.evidence}</div>
      </div>
      <div className="cands">
        {cands.map((c, i) => (
          <button
            key={c.id}
            className={`cand ${chosen?.inhouse_id === c.id ? "sel" : ""}`}
            onClick={() => onPick(row, c.id, c.name)}
            title={c.id}
          >
            <span className="rank">{i + 1}</span>
            <span className="cname">{c.name}</span>
            <span className="score">{c.score != null ? c.score.toFixed(2) : ""}</span>
          </button>
        ))}
        <div className="cand-actions">
          <button className={`nomatch ${chosen && chosen.inhouse_id === null ? "sel" : ""}`} onClick={() => onPick(row, null, "No match")}>
            No match
          </button>
          <button className="link" onClick={() => setSearching(!searching)}>
            {searching ? "Close search" : "Search in-house…"}
          </button>
        </div>
        {searching && (
          <div className="searchbox">
            <input autoFocus placeholder="Name or ID" value={sq} onChange={(e) => setSq(e.target.value)} />
            {hits.map((h) => (
              <button key={h.inhouse_id} className="hit" onClick={() => { onPick(row, h.inhouse_id, h.name); setSearching(false); }}>
                {h.name} <span className="hint">{h.inhouse_id} · {h.country}</span>
              </button>
            ))}
          </div>
        )}
        {chosen && chosen.inhouse_id && !cands.some((c) => c.id === chosen.inhouse_id) && (
          <div className="hint">Chosen: {chosen.label}</div>
        )}
      </div>
    </div>
  );
}
