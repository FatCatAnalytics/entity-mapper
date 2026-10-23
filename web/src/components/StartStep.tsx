import { useEffect, useRef, useState } from "react";
import { api, Startup, SyncResult, waitForJob } from "../api";

type Tone = "ok" | "warn" | "bad" | "busy" | "idle";

function Check({ tone, title, children }: { tone: Tone; title: string; children?: React.ReactNode }) {
  const mark = { ok: "✓", warn: "!", bad: "×", busy: "", idle: "–" }[tone];
  return (
    <li className={`check-row ${tone}`}>
      <span className="mark" aria-hidden>{mark}</span>
      <div>
        <div className="check-title">{title}</div>
        {children && <div className="hint">{children}</div>}
      </div>
    </li>
  );
}

const n = (x?: number) => (x ?? 0).toLocaleString();

export function StartStep({
  status,
  onStatus,
  onContinue,
  onGo,
}: {
  status: Startup | null;
  onStatus: (s: Startup) => void;
  onContinue: () => void;
  onGo: (tab: "ref" | "owner" | "memory" | "period" | "match") => void;
}) {
  const [stage, setStage] = useState("");
  const [err, setErr] = useState("");
  const [folder, setFolder] = useState("");
  const [folderErr, setFolderErr] = useState("");
  const [editing, setEditing] = useState(false);
  const started = useRef(false);

  async function check() {
    setErr("");
    setStage("Checking");
    try {
      const { job_id } = await api.sync();
      await waitForJob<SyncResult>(job_id, (j) => setStage(j.stage));
      onStatus(await api.startup());
    } catch (e) {
      setErr((e as Error).message);
      api.startup().then(onStatus).catch(() => undefined);
    } finally {
      setStage("");
    }
  }

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    // Already checked since the engine started (back on this tab, or the page was reloaded):
    // show that result instead of checking again.
    api
      .startup()
      .then((s) => {
        onStatus(s);
        if (!s.sync) check();
      })
      .catch((e) => setErr((e as Error).message));
  }, []);

  async function saveFolder() {
    setFolderErr("");
    try {
      onStatus(await api.saveSettings(folder));
      setEditing(false);
      await check();
    } catch (e) {
      setFolderErr((e as Error).message);
    }
  }

  if (!status) {
    return (
      <section className="card">
        <ul className="checks">
          <Check tone={err ? "bad" : "busy"} title={err ? "The engine did not answer" : "Starting"}>{err}</Check>
        </ul>
      </section>
    );
  }

  const busy = !!stage;
  const { shared, reference, master, sync } = status;
  const loaded = reference.loaded;
  const pub = loaded?.published ?? null;
  const conflicts = sync?.conflicts?.length ?? 0;
  const showFolderForm = (!shared.configured || editing) && !shared.from_env;

  return (
    <div className="stack">
      <section className="card">
        <h3>Before you match</h3>
        <ul className="checks">
          <Check tone="ok" title="Engine running on this computer">
            Signed in as {status.user} · workspace {status.workspace}
          </Check>

          <Check
            tone={!shared.configured ? "warn" : shared.reachable ? "ok" : "warn"}
            title={
              !shared.configured
                ? "Shared folder not set"
                : shared.reachable
                  ? "Shared folder connected"
                  : "Shared folder not reachable"
            }
          >
            {!shared.configured && "The reference, the master mapping and everyone's decisions are kept in one team folder. Ask the owner for its path."}
            {shared.configured && shared.reachable && (
              <>
                {shared.path} · owner: {shared.owner || "not set (whoever publishes first)"}{" "}
                {!shared.from_env && !editing && <button className="link" onClick={() => { setFolder(shared.path); setEditing(true); }}>change</button>}
              </>
            )}
            {shared.configured && !shared.reachable && (
              <>
                {shared.path} cannot be opened (drive not connected?). Working from the last copy on this computer; your
                decisions are sent when it is back.{" "}
                {!shared.from_env && !editing && <button className="link" onClick={() => { setFolder(shared.path); setEditing(true); }}>change</button>}
              </>
            )}
            {showFolderForm && (
              <span className="inline-form">
                <input
                  value={folder}
                  onChange={(e) => setFolder(e.target.value)}
                  placeholder={"e.g. \\\\server\\team\\EntityMapper"}
                  aria-label="Shared folder path"
                  onKeyDown={(e) => e.key === "Enter" && folder.trim() && saveFolder()}
                />
                <button className="primary" disabled={!folder.trim() || busy} onClick={saveFolder}>Save</button>
                {editing && <button className="link" onClick={() => setEditing(false)}>cancel</button>}
                {folderErr && <span className="error">{folderErr}</span>}
              </span>
            )}
          </Check>

          <Check
            tone={stage === "Loading the reference" ? "busy" : loaded ? (sync?.offline ? "warn" : "ok") : busy ? "busy" : "bad"}
            title={
              loaded
                ? pub
                  ? `Reference loaded: published ${pub.version} by ${pub.published_by}`
                  : `Reference loaded from ${loaded.universe_file ?? "a file you chose"}`
                : busy
                  ? "Reference"
                  : "No reference yet"
            }
          >
            {loaded && (
              <>
                {n(loaded.inhouse_entities)} entities · {n(loaded.gleif_subsidiaries)} subsidiaries · {n(loaded.leis_indexed)} LEIs ·{" "}
                {n(loaded.ciq_ids_indexed)} CIQ IDs
                {sync?.copied ? " · updated to the newly published version" : ""}
                {sync?.offline ? " · could not check for a newer one" : ""}
              </>
            )}
            {!loaded && !busy && shared.reachable && (
              <>
                Nothing has been published to the shared folder. The owner builds and publishes it on the{" "}
                <button className="link" onClick={() => onGo("owner")}>Owner</button> tab.
              </>
            )}
            {!loaded && !busy && !shared.reachable && (
              <>
                Set the shared folder above, or load a universe master file by hand on{" "}
                <button className="link" onClick={() => onGo("ref")}>Reference data</button>.
              </>
            )}
          </Check>

          <Check
            tone={busy && !sync ? "busy" : conflicts || sync?.push_error ? "warn" : sync?.shared ? "ok" : "idle"}
            title={sync?.shared ? "Master mapping and decisions in step with the team" : "Master mapping and decisions"}
          >
            {n(master.imported_rows)} previous {master.imported_rows === 1 ? "mapping" : "mappings"} · {n(master.reviewer_decisions)} reviewer{" "}
            {master.reviewer_decisions === 1 ? "decision" : "decisions"}
            {!sync?.shared ? " on this computer only" : sync.users ? ` from ${n(sync.users)} ${sync.users === 1 ? "person" : "people"}` : ""}
            {sync?.pushed ? ` · ${n(sync.pushed)} of yours sent just now` : ""}
            {sync?.push_error && <><br />Your decisions are saved here but could not be written to the shared folder ({sync.push_error}).</>}
            {conflicts > 0 && (
              <>
                <br />
                {n(conflicts)} {conflicts === 1 ? "name has" : "names have"} conflicting decisions; the latest is used until the owner
                settles {conflicts === 1 ? "it" : "them"}. <button className="link" onClick={() => onGo("memory")}>See them</button>
              </>
            )}
          </Check>
        </ul>

        <div className="actions">
          <button className="primary" disabled={!loaded || busy} onClick={onContinue}>
            Match a bank file
          </button>
          <button className="button" disabled={busy} onClick={check}>
            {busy ? "Checking…" : "Check again"}
          </button>
          {busy && <span className="hint">{stage}…</span>}
          {err && <span className="error">{err}</span>}
        </div>
      </section>
    </div>
  );
}
