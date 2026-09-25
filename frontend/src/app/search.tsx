"use client";

import { useState, type FormEvent } from "react";

// Amplify rewrites /api/* to App Runner, so this is same-origin in the browser.
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api";

// Aurora auto-pauses when idle; the first query after a pause gets a 503 while it wakes.
const RESUME_RETRY_MS = 5000;
const RESUME_ATTEMPTS = 12;

// The API cuts abstracts to this many characters.
const SNIPPET_LENGTH = 400;

const PHASES = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"];

type Site = {
  facility: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  status: string | null;
};

// Requirements a model read from the criteria; null where the trial sets none.
type Eligibility = {
  setting: "newly_diagnosed" | "recurrent" | null;
  max_recurrence: number | null;
  idh: "wildtype" | "mutant" | null;
  mgmt: "methylated" | "unmethylated" | null;
  prior_bevacizumab: "excluded" | "required" | null;
  min_kps: number | null;
};

type Trial = {
  nct_id: string;
  title: string;
  overall_status: string;
  phases: string[];
  conditions: string[];
  sponsor: string | null;
  min_age_years: number | null;
  max_age_years: number | null;
  last_update_posted: string;
  site_count: number;
  sites: Site[];
  eligibility: Eligibility | null;
};

type Paper = {
  pmid: string;
  title: string;
  journal: string | null;
  pub_date: string | null;
  pub_types: string[];
  doi: string | null;
  snippet: string | null;
  nct_ids: string[];
};

type Results = { query: string; trials: Trial[]; papers: Paper[]; kind: Filters["kind"] };

type Filters = {
  kind: "all" | "trials" | "papers";
  recruiting: boolean;
  phase: string;
  age: string;
  country: string;
  state: string;
  city: string;
  idh: string;
  mgmt: string;
  setting: string;
  recurrence: string;
  priorBevacizumab: string;
  kps: string;
  fromYear: string;
};

const NO_FILTERS: Filters = {
  kind: "all",
  recruiting: false,
  phase: "",
  age: "",
  country: "",
  state: "",
  city: "",
  idh: "",
  mgmt: "",
  setting: "",
  recurrence: "",
  priorBevacizumab: "",
  kps: "",
  fromYear: "",
};

function label(value: string) {
  return value.replaceAll("_", " ").toLowerCase();
}

function requirements(e: Eligibility) {
  return [
    e.setting && label(e.setting),
    e.max_recurrence && `up to recurrence ${e.max_recurrence}`,
    e.idh && `IDH ${e.idh}`,
    e.mgmt && `MGMT ${e.mgmt}`,
    e.prior_bevacizumab && `prior bevacizumab ${e.prior_bevacizumab}`,
    e.min_kps != null && `KPS ${e.min_kps}+`,
  ].filter(Boolean);
}

function ageRange(min: number | null, max: number | null) {
  if (min == null && max == null) return "any age";
  if (max == null) return `${min}+`;
  if (min == null) return `up to ${max}`;
  return `${min} to ${max}`;
}

export default function Search() {
  const [q, setQ] = useState("");
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [results, setResults] = useState<Results | null>(null);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);

  function set<K extends keyof Filters>(key: K, value: Filters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  async function run(query: string, f: Filters) {
    if (query.trim().length < 2) return;
    setBusy(true);
    setStatus("");
    const params = new URLSearchParams({ q: query.trim(), kind: f.kind, limit: "15" });
    if (f.recruiting) params.set("recruiting", "true");
    if (f.phase) params.set("phase", f.phase);
    if (f.age) params.set("age", f.age);
    if (f.country) params.set("country", f.country);
    if (f.state) params.set("state", f.state);
    if (f.city) params.set("city", f.city);
    if (f.idh) params.set("idh", f.idh);
    if (f.mgmt) params.set("mgmt", f.mgmt);
    if (f.setting) params.set("setting", f.setting);
    if (f.recurrence) params.set("recurrence", f.recurrence);
    if (f.priorBevacizumab) params.set("prior_bevacizumab", f.priorBevacizumab);
    if (f.kps) params.set("kps", f.kps);
    if (f.fromYear) params.set("from_year", f.fromYear);

    try {
      for (let attempt = 0; attempt < RESUME_ATTEMPTS; attempt++) {
        const response = await fetch(`${API_URL}/search?${params}`);
        if (response.status === 503) {
          setStatus("The database is waking up. This takes up to a minute.");
          await new Promise((resolve) => setTimeout(resolve, RESUME_RETRY_MS));
          continue;
        }
        if (!response.ok) {
          setStatus(`Search failed (${response.status}).`);
          return;
        }
        setResults({ ...(await response.json()), kind: f.kind });
        setStatus("");
        return;
      }
      setStatus("The database did not wake up. Try again in a minute.");
    } catch (error) {
      setStatus(`Search failed: ${String(error)}`);
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    run(q, filters);
  }

  function searchTrial(nctId: string) {
    const f = { ...NO_FILTERS, kind: "trials" as const };
    setQ(nctId);
    setFilters(f);
    run(nctId, f);
  }

  const input =
    "rounded border border-black/15 dark:border-white/20 bg-transparent px-2 py-1 text-sm";

  return (
    <section className="space-y-6">
      <form onSubmit={submit} className="space-y-3">
        <div className="flex gap-2">
          <input
            value={q}
            onChange={(event) => setQ(event.target.value)}
            placeholder="Search trials and papers, e.g. dendritic cell vaccine, NCT00916409"
            aria-label="Search"
            className="flex-1 rounded border border-black/15 dark:border-white/20 bg-transparent px-3 py-2"
          />
          <button
            type="submit"
            disabled={busy || q.trim().length < 2}
            className="rounded bg-foreground px-4 py-2 text-sm text-background disabled:opacity-50"
          >
            {busy ? "Searching…" : "Search"}
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
          <select
            value={filters.kind}
            onChange={(event) => set("kind", event.target.value as Filters["kind"])}
            aria-label="Search in"
            className={input}
          >
            <option value="all">Trials and papers</option>
            <option value="trials">Trials only</option>
            <option value="papers">Papers only</option>
          </select>
          {filters.kind !== "papers" && (
            <>
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={filters.recruiting}
                  onChange={(event) => set("recruiting", event.target.checked)}
                />
                Recruiting
              </label>
              <select
                value={filters.phase}
                onChange={(event) => set("phase", event.target.value)}
                aria-label="Phase"
                className={input}
              >
                <option value="">Any phase</option>
                {PHASES.map((phase) => (
                  <option key={phase} value={phase}>
                    {label(phase)}
                  </option>
                ))}
              </select>
              <input
                type="number"
                min={0}
                max={120}
                value={filters.age}
                onChange={(event) => set("age", event.target.value)}
                placeholder="Age"
                aria-label="Age"
                className={`${input} w-20`}
              />
              <input
                value={filters.country}
                onChange={(event) => set("country", event.target.value)}
                placeholder="Country"
                aria-label="Country"
                className={`${input} w-36`}
              />
              <input
                value={filters.state}
                onChange={(event) => set("state", event.target.value)}
                placeholder="State"
                aria-label="State"
                className={`${input} w-32`}
              />
              <input
                value={filters.city}
                onChange={(event) => set("city", event.target.value)}
                placeholder="City"
                aria-label="City"
                className={`${input} w-32`}
              />
              <select
                value={filters.setting}
                onChange={(event) => set("setting", event.target.value)}
                aria-label="Disease setting"
                className={input}
              >
                <option value="">Newly diagnosed or recurrent</option>
                <option value="newly_diagnosed">Newly diagnosed</option>
                <option value="recurrent">Recurrent</option>
              </select>
              <input
                type="number"
                min={1}
                max={10}
                value={filters.recurrence}
                onChange={(event) => set("recurrence", event.target.value)}
                placeholder="Recurrence no."
                aria-label="Recurrence number"
                className={`${input} w-32`}
              />
              <select
                value={filters.idh}
                onChange={(event) => set("idh", event.target.value)}
                aria-label="IDH status"
                className={input}
              >
                <option value="">Any IDH</option>
                <option value="wildtype">IDH wildtype</option>
                <option value="mutant">IDH mutant</option>
              </select>
              <select
                value={filters.mgmt}
                onChange={(event) => set("mgmt", event.target.value)}
                aria-label="MGMT status"
                className={input}
              >
                <option value="">Any MGMT</option>
                <option value="methylated">MGMT methylated</option>
                <option value="unmethylated">MGMT unmethylated</option>
              </select>
              <select
                value={filters.priorBevacizumab}
                onChange={(event) => set("priorBevacizumab", event.target.value)}
                aria-label="Prior bevacizumab"
                className={input}
              >
                <option value="">Prior bevacizumab: either</option>
                <option value="true">Had bevacizumab</option>
                <option value="false">No bevacizumab</option>
              </select>
              <input
                type="number"
                min={0}
                max={100}
                step={10}
                value={filters.kps}
                onChange={(event) => set("kps", event.target.value)}
                placeholder="KPS"
                aria-label="Karnofsky performance status"
                className={`${input} w-20`}
              />
            </>
          )}
          {filters.kind !== "trials" && (
            <input
              type="number"
              min={1900}
              max={2100}
              value={filters.fromYear}
              onChange={(event) => set("fromYear", event.target.value)}
              placeholder="Papers from year"
              aria-label="Papers from year"
              className={`${input} w-36`}
            />
          )}
        </div>
      </form>

      {status && <p className="text-sm text-black/60 dark:text-white/60">{status}</p>}

      {results && (
        <div className="space-y-8">
          {results.kind !== "papers" && (
            <TrialList
              trials={results.trials}
              located={Boolean(filters.country || filters.state || filters.city)}
            />
          )}
          {results.kind !== "trials" && (
            <PaperList papers={results.papers} onTrial={searchTrial} />
          )}
        </div>
      )}
    </section>
  );
}

function TrialList({ trials, located }: { trials: Trial[]; located: boolean }) {
  return (
    <div className="space-y-3">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-black/60 dark:text-white/60">
        Trials ({trials.length})
      </h2>
      {trials.length === 0 && <p className="text-sm">No matching trials.</p>}
      <ul className="space-y-4">
        {trials.map((trial) => (
          <li key={trial.nct_id} className="space-y-1">
            <a
              href={`https://clinicaltrials.gov/study/${trial.nct_id}`}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium underline-offset-4 hover:underline"
            >
              {trial.title}
            </a>
            <p className="text-xs text-black/60 dark:text-white/60">
              {trial.nct_id} · {label(trial.overall_status)}
              {trial.phases.length > 0 && ` · ${trial.phases.map(label).join(", ")}`}
              {` · ages ${ageRange(trial.min_age_years, trial.max_age_years)}`}
              {trial.sponsor && ` · ${trial.sponsor}`}
            </p>
            {trial.eligibility && requirements(trial.eligibility).length > 0 && (
              <p className="text-xs">
                Requires: {requirements(trial.eligibility).join(" · ")}{" "}
                <span className="text-black/60 dark:text-white/60">
                  (read from the criteria by a model; check the full criteria)
                </span>
              </p>
            )}
            {trial.sites.length > 0 && (
              <p className="text-xs">
                {located ? "Sites here: " : "Sites: "}
                {trial.sites
                  .map((site) => [site.facility, site.city, site.state].filter(Boolean).join(", "))
                  .join("; ")}
                {trial.site_count > trial.sites.length &&
                  ` (${trial.site_count} sites in total)`}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function PaperList({ papers, onTrial }: { papers: Paper[]; onTrial: (nctId: string) => void }) {
  return (
    <div className="space-y-3">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-black/60 dark:text-white/60">
        Papers ({papers.length})
      </h2>
      {papers.length === 0 && <p className="text-sm">No matching papers.</p>}
      <ul className="space-y-4">
        {papers.map((paper) => (
          <li key={paper.pmid} className="space-y-1">
            <a
              href={`https://pubmed.ncbi.nlm.nih.gov/${paper.pmid}/`}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium underline-offset-4 hover:underline"
            >
              {paper.title}
            </a>
            <p className="text-xs text-black/60 dark:text-white/60">
              PMID {paper.pmid}
              {paper.journal && ` · ${paper.journal}`}
              {paper.pub_date && ` · ${paper.pub_date.slice(0, 4)}`}
            </p>
            {paper.snippet && (
              <p className="text-sm">
                {paper.snippet}
                {paper.snippet.length >= SNIPPET_LENGTH && "…"}
              </p>
            )}
            {paper.nct_ids.length > 0 && (
              <p className="flex flex-wrap gap-2 text-xs">
                Trials:
                {paper.nct_ids.map((nctId) => (
                  <button
                    key={nctId}
                    type="button"
                    onClick={() => onTrial(nctId)}
                    className="underline underline-offset-4 hover:no-underline"
                  >
                    {nctId}
                  </button>
                ))}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
