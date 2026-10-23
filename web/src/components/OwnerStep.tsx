import { useState } from "react";
import { api, Built, GleifLatest, Job, Manifest, Startup, Upload, waitForJob } from "../api";
import { FilePicker } from "./FilePicker";

const mb = (b: number) => `${Math.round(b / 1e6).toLocaleString()} MB`;

export function OwnerStep({ status, onPublished }: { status: Startup | null; onPublished: () => void }) {
  const [upload, setUpload] = useState<Upload | null>(null);
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [source, setSource] = useState<"download" | "files">("download");
  const [latest, setLatest] = useState<GleifLatest | null>(null);
  const [latestErr, setLatestErr] = useState("");
  const [l1, setL1] = useState("");
  const [rr, setRr] = useState("");
  const [sp, setSp] = useState(false);
  const [job, setJob] = useState<Job<Built> | null>(null);
  const [built, setBuilt] = useState<Built | null>(null);
  const [err, setErr] = useState("");
  const [pubErr, setPubErr] = useState("");
  const [published, setPublished] = useState<Manifest | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [existing, setExisting] = useState<Upload | null>(null);

  const shared = status?.shared;
  const blocked = !shared?.reachable
    ? "Set the shared folder on the Start tab first: that is where the reference is published."
    : shared.owner && !shared.is_owner
      ? `Only the owner (${shared.owner}) builds and publishes the reference. Everyone else's app picks it up on start.`
      : "";

  async function checkLatest() {
    setLatestErr("");
    try {
      setLatest(await api.gleifLatest());
    } catch (e) {
      setLatestErr((e as Error).message);
      setSource("files");
    }
  }

  async function build() {
    if (!upload) return;
    setErr("");
    setBuilt(null);
    setPublished(null);
    try {
      const { job_id } = await api.ownerBuild({
        inhouse_file_id: upload.file_id,
        mapping,
        sp_ids: sp,
        lei2_path: source === "files" ? l1 : undefined,
        rr_path: source === "files" ? rr : undefined,
      });
      setBuilt(await waitForJob<Built>(job_id, setJob));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setJob(null);
    }
  }

  async function publish(fileId?: string) {
    setPubErr("");
    setPublishing(true);
    try {
      setPublished(await api.ownerPublish(fileId));
      onPublished();
    } catch (e) {
      setPubErr((e as Error).message);
    } finally {
      setPublishing(false);
    }
  }

  if (blocked) {
    return (
      <section className="card">
        <h3>Owner: build and publish the reference</h3>
        <p className="hint">{blocked}</p>
      </section>
    );
  }

  const running = !!job;
  const current = status?.reference.published;

  return (
    <div className="stack">
      <section className="card">
        <h3>Owner: build and publish the reference</h3>
        <p className="hint" style={{ marginTop: 0 }}>
          Monthly, or when the universe changes. Build the universe master from your list and the GLEIF Golden Copy,
          review the LEI decisions, then publish. Everyone's app loads the published version on its next start.
          {current ? ` Current: ${current.version} by ${current.published_by}.` : " Nothing is published yet."}
        </p>
      </section>

      <FilePicker
        kind="inhouse"
        title="1 · Your universe list"
        required={["inhouse_id", "name"]}
        upload={upload}
        mapping={mapping}
        onUpload={setUpload}
        onMapping={setMapping}
      />

      <section className="card">
        <h3>2 · GLEIF Golden Copy</h3>
        <label className="check" style={{ marginTop: 0 }}>
          <input type="radio" name="gleif" checked={source === "download"} onChange={() => setSource("download")} />
          Download the latest from gleif.org (public data; about 500 MB, resumes if interrupted)
        </label>
        {source === "download" && (
          <p className="hint indent">
            {latest ? (
              <>
                Published {latest.publish_date}: entities {mb(latest.lei2.size)}
                {latest.lei2.downloaded ? " (already here)" : ""}, relationships {mb(latest.rr.size)}
                {latest.rr.downloaded ? " (already here)" : ""}.
              </>
            ) : (
              <button className="link" onClick={checkLatest}>Check what is available</button>
            )}
          </p>
        )}
        {latestErr && <p className="error indent">{latestErr}</p>}
        <label className="check">
          <input type="radio" name="gleif" checked={source === "files"} onChange={() => setSource("files")} />
          Use files I downloaded myself
        </label>
        {source === "files" && (
          <div className="indent">
            <label className="field">
              <span>Level 1 entities (…-lei2-golden-copy.csv.zip): full path on this computer</span>
              <input value={l1} onChange={(e) => setL1(e.target.value)} aria-label="Level 1 file path" />
            </label>
            <label className="field">
              <span>Level 2 relationships (…-rr-golden-copy.csv.zip): full path on this computer</span>
              <input value={rr} onChange={(e) => setRr(e.target.value)} aria-label="Level 2 file path" />
            </label>
          </div>
        )}
        <label className="check">
          <input type="checkbox" checked={sp} onChange={(e) => setSp(e.target.checked)} />
          Add S&amp;P Capital IQ IDs from the GLEIF API. Sends your LEIs and CIQ numbers only, never names; about 10 minutes
        </label>
      </section>

      <div className="actions">
        <button className="primary" disabled={!upload || running || (source === "files" && (!l1.trim() || !rr.trim()))} onClick={build}>
          {running ? "Building…" : "Build universe master"}
        </button>
        {err && <span className="error">{err}</span>}
      </div>

      {job && (
        <section className="card">
          <h3>{job.stage}…</h3>
          <div className={`bar ${job.progress == null ? "indeterminate" : ""}`}>
            <div style={{ width: `${Math.round((job.progress ?? 1) * 100)}%` }} />
          </div>
          {job.log.length > 0 && <pre className="joblog">{job.log.slice(-4).join("\n")}</pre>}
        </section>
      )}

      {built && (
        <section className="card ok">
          <h3>3 · Review, then publish · {built.file}</h3>
          <table className="tiers">
            <tbody>
              {built.summary.map((r) => (
                <tr key={r.label}>
                  <td className={r.sub ? "sub" : ""}>{r.label}</td>
                  <td>{/^\d+$/.test(r.value) ? Number(r.value).toLocaleString() : r.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint">
            {built.lei_decisions.toLocaleString()} LEI decisions to look over on the "LEI decisions" sheet (LEIs replaced,
            added or not used). A wrong LEI pulls a whole wrong group into matching.
          </p>
          <div className="actions">
            <a className="button" href={api.builtFileUrl}>Download to review</a>
            <button className="primary" disabled={publishing || !!published} onClick={() => publish()}>
              {published ? "Published" : publishing ? "Publishing…" : "Publish to the shared folder"}
            </button>
            {pubErr && <span className="error">{pubErr}</span>}
          </div>
        </section>
      )}

      {published && (
        <section className="card ok">
          <p className="okmsg">
            Published {published.version}. This app has loaded it; everyone else gets it on their next start.
          </p>
        </section>
      )}

      <section className="card">
        <h3>Or publish a universe master file you already have</h3>
        <p className="hint" style={{ marginTop: 0 }}>For a file you edited after review, or one built with the script.</p>
        <input
          type="file"
          accept=".xlsx"
          data-testid="file-publish"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            if (!f) return;
            setPubErr("");
            try {
              setExisting(await api.upload(f, "universe"));
            } catch (x) {
              setPubErr((x as Error).message);
            }
          }}
        />
        {existing && (
          <div className="actions" style={{ marginTop: 10 }}>
            <span className="hint">{existing.filename} · {existing.rows.toLocaleString()} rows</span>
            <button className="primary" disabled={publishing} onClick={() => publish(existing.file_id)}>
              Publish this file
            </button>
          </div>
        )}
        {!built && pubErr && <p className="error">{pubErr}</p>}
      </section>
    </div>
  );
}
