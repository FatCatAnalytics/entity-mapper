import { useEffect, useState } from "react";
import { api, RefInfo, RunStatus, Startup, waitForJob } from "./api";
import { MatchStep } from "./components/MatchStep";
import { MemoryStep } from "./components/MemoryStep";
import { OwnerStep } from "./components/OwnerStep";
import { PeriodStep } from "./components/PeriodStep";
import { ReferenceStep } from "./components/ReferenceStep";
import { ReviewStep } from "./components/ReviewStep";
import { StartStep } from "./components/StartStep";

type Tab = "start" | "match" | "period" | "review" | "ref" | "memory" | "owner";

export default function App() {
  const [tab, setTab] = useState<Tab>("start");
  const [status, setStatus] = useState<Startup | null>(null);
  const [ref, setRef] = useState<RefInfo | null>(null);
  const [run, setRun] = useState<RunStatus | null>(null);

  function onStatus(s: Startup) {
    setStatus(s);
    setRef(s.reference.loaded);
  }
  const refresh = () => api.startup().then(onStatus).catch(() => undefined);

  useEffect(() => {
    refresh();
  }, []);

  const conflicts = status?.sync?.conflicts?.length ?? 0;
  const tabs: [Tab, string, string][] = [
    ["start", "1", "Start"],
    ["period", "2", "Period map"],
    ["match", "3", "Match a bank file"],
    ["review", "4", "Review"],
    ["ref", "", "Reference data"],
    ["memory", "", "Alias memory"],
    ["owner", "", "Owner"],
  ];

  return (
    <div className="app">
      <header>
        <div className="brand">
          <span className="logo">EM</span>
          <div>
            <h1>Entity Mapper</h1>
            <p>Bank submission to in-house list · runs locally, no client data leaves this machine</p>
          </div>
        </div>
        <nav>
          {tabs.map(([k, n, l]) => (
            <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
              {n && <span className="n">{n}</span>}
              {l}
              {k === "start" && ref && <span className="dot" />}
              {k === "review" && run?.summary?.review ? <span className="count">{run.summary.review.toLocaleString()}</span> : null}
              {k === "memory" && conflicts ? <span className="count">{conflicts.toLocaleString()}</span> : null}
            </button>
          ))}
        </nav>
      </header>
      <main>
        {tab === "start" && (
          <StartStep status={status} onStatus={onStatus} onContinue={() => setTab("period")} onGo={setTab} />
        )}
        {tab === "period" && <PeriodStep run={run} onRun={setRun} onReview={() => setTab("review")} />}
        {tab === "match" && <MatchStep ready={!!ref} run={run} onRun={setRun} onReview={() => setTab("review")} />}
        {tab === "review" && <ReviewStep run={run} onSummary={(s) => run && setRun({ ...run, summary: s })} />}
        {tab === "ref" && <ReferenceStep info={ref} onBuilt={(r) => { setRef(r); refresh(); }} />}
        {tab === "memory" && <MemoryStep status={status} onStatus={onStatus} />}
        {tab === "owner" && (
          <OwnerStep
            status={status}
            onPublished={async () => {
              const { job_id } = await api.sync();
              await waitForJob(job_id).catch(() => undefined);
              refresh();
            }}
          />
        )}
      </main>
    </div>
  );
}
