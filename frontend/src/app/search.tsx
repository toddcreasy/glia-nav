"use client";

import { useState, type FormEvent, type ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

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

// ClinicalTrials.gov statuses in the words a patient would use, and how loud to show them.
const STATUS: Record<string, { text: string; tone: "open" | "closed" | "ended" }> = {
  RECRUITING: { text: "Taking new patients", tone: "open" },
  NOT_YET_RECRUITING: { text: "Not open yet", tone: "closed" },
  ENROLLING_BY_INVITATION: { text: "By invitation only", tone: "closed" },
  ACTIVE_NOT_RECRUITING: { text: "Not taking new patients", tone: "closed" },
  SUSPENDED: { text: "Paused", tone: "closed" },
  AVAILABLE: { text: "Expanded access available", tone: "open" },
  NO_LONGER_AVAILABLE: { text: "Expanded access closed", tone: "ended" },
  COMPLETED: { text: "Completed", tone: "ended" },
  TERMINATED: { text: "Stopped early", tone: "ended" },
  WITHDRAWN: { text: "Withdrawn before starting", tone: "ended" },
  UNKNOWN: { text: "Status not updated", tone: "ended" },
};

const TONE = {
  open: "bg-success/10 text-success",
  closed: "bg-warning-soft text-warning",
  ended: "bg-muted text-muted-foreground",
};

function phaseText(phases: string[]) {
  const numbered = phases.filter((p) => p !== "NA");
  if (numbered.length === 0) return null;
  if (numbered[0] === "EARLY_PHASE1") return "Early phase 1";
  return `Phase ${numbered.map((p) => p.replace("PHASE", "")).join("/")}`;
}

function ageText(min: number | null, max: number | null) {
  if (min == null && max == null) return "Any age";
  if (max == null) return `Age ${min}+`;
  if (min == null) return `Up to age ${max}`;
  return `Ages ${min} to ${max}`;
}

function whoCanJoin(trial: Trial) {
  const e = trial.eligibility;
  const lines: string[] = [];
  if (e?.setting === "newly_diagnosed") lines.push("Newly diagnosed");
  if (e?.setting === "recurrent") {
    lines.push(
      e.max_recurrence === 1
        ? "Tumor came back for the first time"
        : e.max_recurrence
          ? `Tumor came back, up to recurrence ${e.max_recurrence}`
          : "Tumor came back after treatment",
    );
  }
  const markers = [e?.idh && `IDH-${e.idh}`, e?.mgmt && `MGMT-${e.mgmt}`].filter(Boolean);
  if (markers.length > 0) lines.push(markers.join(" and "));
  if (e?.prior_bevacizumab === "excluded") lines.push("Has not had bevacizumab (Avastin)");
  if (e?.prior_bevacizumab === "required") lines.push("Has had bevacizumab (Avastin)");
  if (e?.min_kps != null) {
    lines.push(`KPS ${e.min_kps} or higher (a score of daily functioning)`);
  }
  lines.push(ageText(trial.min_age_years, trial.max_age_years));
  return lines;
}

const MONTH_YEAR = new Intl.DateTimeFormat("en-US", {
  month: "long",
  year: "numeric",
  timeZone: "UTC",
});

function published(date: string | null) {
  if (!date) return null;
  return MONTH_YEAR.format(new Date(`${date.slice(0, 10)}T00:00:00Z`));
}

// Filters behind "More filters"; the chips cover the rest.
const MORE: (keyof Filters)[] = [
  "phase",
  "age",
  "country",
  "state",
  "city",
  "recurrence",
  "idh",
  "mgmt",
  "priorBevacizumab",
  "kps",
  "fromYear",
];

const SELECT =
  "h-9 w-full rounded-md border border-input bg-card px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function Chip({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`h-9 rounded-full border px-3.5 text-sm transition-colors ${
        on
          ? "border-primary bg-secondary text-secondary-foreground"
          : "border-input bg-card text-foreground hover:bg-muted"
      }`}
    >
      {children}
    </button>
  );
}

export default function Search() {
  const [q, setQ] = useState("");
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [results, setResults] = useState<Results | null>(null);
  const [status, setStatus] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [showMore, setShowMore] = useState(false);

  function set<K extends keyof Filters>(key: K, value: Filters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  // Chips apply at once: with results on screen, they search again.
  function apply<K extends keyof Filters>(key: K, value: Filters[K]) {
    const f = { ...filters, [key]: value };
    setFilters(f);
    if (results) run(q, f);
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

  const moreCount = MORE.filter((key) => filters[key] !== "").length;
  const trialsOn = filters.kind !== "papers";

  return (
    <section id="search" aria-label="Search" className="scroll-mt-6 space-y-6">
      <form onSubmit={submit} className="space-y-4 rounded-2xl border bg-card p-5">
        <label htmlFor="q" className="block font-semibold">
          What are you looking for?
        </label>
        <div className="flex gap-2.5">
          <Input
            id="q"
            type="search"
            value={q}
            onChange={(event) => setQ(event.target.value)}
            placeholder="A treatment, a trial ID, or a question, e.g. dendritic cell vaccine"
            className="h-12 rounded-xl bg-card px-4 text-base md:text-base"
          />
          <Button
            type="submit"
            disabled={busy || q.trim().length < 2}
            className="h-12 rounded-xl px-5 text-base font-semibold"
          >
            {busy ? "Searching…" : "Search"}
          </Button>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <div
            role="group"
            aria-label="Search in"
            className="mr-2 flex rounded-full border border-input bg-muted p-0.5 text-sm"
          >
            {(
              [
                ["all", "Everything"],
                ["trials", "Trials"],
                ["papers", "Papers"],
              ] as const
            ).map(([kind, text]) => (
              <button
                key={kind}
                type="button"
                aria-pressed={filters.kind === kind}
                onClick={() => apply("kind", kind)}
                className={`h-8 rounded-full px-3 ${
                  filters.kind === kind
                    ? "bg-card font-medium text-foreground shadow-xs"
                    : "text-muted-foreground"
                }`}
              >
                {text}
              </button>
            ))}
          </div>
          {trialsOn && (
            <>
              <Chip on={filters.recruiting} onClick={() => apply("recruiting", !filters.recruiting)}>
                Taking new patients
              </Chip>
              <Chip
                on={filters.setting === "newly_diagnosed"}
                onClick={() =>
                  apply("setting", filters.setting === "newly_diagnosed" ? "" : "newly_diagnosed")
                }
              >
                Newly diagnosed
              </Chip>
              <Chip
                on={filters.setting === "recurrent"}
                onClick={() =>
                  apply("setting", filters.setting === "recurrent" ? "" : "recurrent")
                }
              >
                Came back after treatment
              </Chip>
            </>
          )}
          <Chip on={showMore} onClick={() => setShowMore(!showMore)}>
            More filters{moreCount > 0 && ` (${moreCount})`}
          </Chip>
        </div>

        {showMore && (
          <div className="grid grid-cols-2 gap-3 border-t pt-4 sm:grid-cols-3">
            {trialsOn && (
              <>
                <Field label="Phase">
                  <select
                    value={filters.phase}
                    onChange={(event) => set("phase", event.target.value)}
                    className={SELECT}
                  >
                    <option value="">Any phase</option>
                    {PHASES.map((phase) => (
                      <option key={phase} value={phase}>
                        {phase === "NA" ? "Not applicable" : phaseText([phase])}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Age">
                  <Input
                    type="number"
                    min={0}
                    max={120}
                    value={filters.age}
                    onChange={(event) => set("age", event.target.value)}
                    className="bg-card"
                  />
                </Field>
                <Field label="Country">
                  <Input
                    value={filters.country}
                    onChange={(event) => set("country", event.target.value)}
                    className="bg-card"
                  />
                </Field>
                <Field label="State">
                  <Input
                    value={filters.state}
                    onChange={(event) => set("state", event.target.value)}
                    className="bg-card"
                  />
                </Field>
                <Field label="City">
                  <Input
                    value={filters.city}
                    onChange={(event) => set("city", event.target.value)}
                    className="bg-card"
                  />
                </Field>
                <Field label="Which recurrence">
                  <Input
                    type="number"
                    min={1}
                    max={10}
                    value={filters.recurrence}
                    onChange={(event) => set("recurrence", event.target.value)}
                    className="bg-card"
                  />
                </Field>
                <Field label="IDH">
                  <select
                    value={filters.idh}
                    onChange={(event) => set("idh", event.target.value)}
                    className={SELECT}
                  >
                    <option value="">Either</option>
                    <option value="wildtype">Wildtype</option>
                    <option value="mutant">Mutant</option>
                  </select>
                </Field>
                <Field label="MGMT">
                  <select
                    value={filters.mgmt}
                    onChange={(event) => set("mgmt", event.target.value)}
                    className={SELECT}
                  >
                    <option value="">Either</option>
                    <option value="methylated">Methylated</option>
                    <option value="unmethylated">Unmethylated</option>
                  </select>
                </Field>
                <Field label="Bevacizumab (Avastin)">
                  <select
                    value={filters.priorBevacizumab}
                    onChange={(event) => set("priorBevacizumab", event.target.value)}
                    className={SELECT}
                  >
                    <option value="">Either</option>
                    <option value="true">Have had it</option>
                    <option value="false">Have not had it</option>
                  </select>
                </Field>
                <Field label="KPS">
                  <Input
                    type="number"
                    min={0}
                    max={100}
                    step={10}
                    value={filters.kps}
                    onChange={(event) => set("kps", event.target.value)}
                    className="bg-card"
                  />
                </Field>
              </>
            )}
            {filters.kind !== "trials" && (
              <Field label="Papers from year">
                <Input
                  type="number"
                  min={1900}
                  max={2100}
                  value={filters.fromYear}
                  onChange={(event) => set("fromYear", event.target.value)}
                  className="bg-card"
                />
              </Field>
            )}
            <div className="col-span-full flex items-center gap-3 text-sm text-muted-foreground">
              <span>These apply when you search.</span>
              {moreCount > 0 && (
                <button
                  type="button"
                  onClick={() =>
                    setFilters((current) => ({
                      ...current,
                      ...Object.fromEntries(MORE.map((key) => [key, ""])),
                    }))
                  }
                  className="text-primary underline-offset-4 hover:underline"
                >
                  Clear these
                </button>
              )}
            </div>
          </div>
        )}
      </form>

      {status && <p className="text-sm text-muted-foreground">{status}</p>}

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

function Heading({ title, count, noun }: { title: string; count: number; noun: string }) {
  return (
    <h2 className="font-serif text-2xl font-medium">
      {title}{" "}
      <span className="font-sans text-[15px] text-muted-foreground">
        {count} {noun}
      </span>
    </h2>
  );
}

function TrialList({ trials, located }: { trials: Trial[]; located: boolean }) {
  return (
    <div className="space-y-4">
      <Heading title="Trials" count={trials.length} noun="found" />
      {trials.length === 0 && (
        <p className="text-muted-foreground">No matching trials. Try fewer filters.</p>
      )}
      {trials.map((trial) => {
        const status = STATUS[trial.overall_status] ?? {
          text: label(trial.overall_status),
          tone: "ended" as const,
        };
        const meta = [
          phaseText(trial.phases),
          trial.sponsor,
          `${trial.site_count} ${trial.site_count === 1 ? "location" : "locations"}`,
        ].filter(Boolean);
        const sites = trial.sites.map((site) =>
          [site.facility, site.city, site.state].filter(Boolean).join(", "),
        );
        return (
          <article key={trial.nct_id} className="space-y-3.5 rounded-2xl border bg-card p-6">
            <div className="flex flex-wrap items-center gap-2.5 text-sm">
              <Badge className={`rounded-full px-2.5 py-1 text-sm font-normal ${TONE[status.tone]}`}>
                <span aria-hidden="true" className="size-2 rounded-full bg-current" />
                {status.text}
              </Badge>
              <span className="text-muted-foreground">{meta.join(" · ")}</span>
            </div>
            <h3 className="font-serif text-xl leading-snug font-semibold">
              <a
                href={`https://clinicaltrials.gov/study/${trial.nct_id}`}
                target="_blank"
                rel="noopener noreferrer"
                className="hover:text-primary"
              >
                {trial.title}
              </a>
            </h3>
            <div className="space-y-2 rounded-xl bg-background p-4">
              <p className="text-[15px] font-semibold">Who can join</p>
              <ul className="list-disc space-y-1 pl-5 text-[15px]">
                {whoCanJoin(trial).map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
              {trial.eligibility && (
                <p className="text-[13px] text-muted-foreground">
                  A model read these from the trial&apos;s criteria. Check the full criteria with
                  the trial team.
                </p>
              )}
            </div>
            {sites.length > 0 && (
              <p className="text-sm text-muted-foreground">
                {located
                  ? "Locations matching your filter: "
                  : trial.site_count === 1
                    ? "Location: "
                    : "Locations include "}
                {sites.join("; ")}
                {trial.site_count > sites.length &&
                  `, and ${trial.site_count - sites.length} more`}
              </p>
            )}
            <a
              href={`https://clinicaltrials.gov/study/${trial.nct_id}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-block text-sm text-primary underline-offset-4 hover:underline"
            >
              View on ClinicalTrials.gov ({trial.nct_id})
            </a>
          </article>
        );
      })}
    </div>
  );
}

function PaperList({ papers, onTrial }: { papers: Paper[]; onTrial: (nctId: string) => void }) {
  return (
    <div className="space-y-4">
      <Heading title="Research" count={papers.length} noun={papers.length === 1 ? "paper" : "papers"} />
      {papers.length === 0 && <p className="text-muted-foreground">No matching papers.</p>}
      {papers.length > 0 && (
        <ul className="divide-y rounded-2xl border bg-card px-6 py-2">
          {papers.map((paper) => (
            <li key={paper.pmid} className="space-y-1.5 py-4">
              <a
                href={`https://pubmed.ncbi.nlm.nih.gov/${paper.pmid}/`}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium hover:text-primary"
              >
                {paper.title}
              </a>
              <p className="text-sm text-muted-foreground">
                {[paper.journal, published(paper.pub_date), `PMID ${paper.pmid}`]
                  .filter(Boolean)
                  .join(", ")}
              </p>
              {paper.snippet && (
                <p className="line-clamp-3 text-sm">
                  {paper.snippet}
                  {paper.snippet.length >= SNIPPET_LENGTH && "…"}
                </p>
              )}
              {paper.nct_ids.length > 0 && (
                <div className="flex flex-wrap items-center gap-2 pt-1 text-sm">
                  <span className="text-muted-foreground">Trials in this paper:</span>
                  {paper.nct_ids.map((nctId) => (
                    <button
                      key={nctId}
                      type="button"
                      onClick={() => onTrial(nctId)}
                      className="rounded-full border border-secondary-foreground/20 bg-secondary px-2.5 py-0.5 text-secondary-foreground hover:border-primary"
                    >
                      {nctId}
                    </button>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
