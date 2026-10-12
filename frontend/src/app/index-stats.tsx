"use client";

import { useEffect, useState } from "react";

type Stats = { trials: number; papers: number; updated_at: string };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api";
const UPDATED = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: "America/New_York",
});

// The daily ingest publishes these. Until it has, nothing shows.
export default function IndexStats() {
  const [stats, setStats] = useState<Stats | null>(null);

  useEffect(() => {
    fetch(`${API_URL}/stats`)
      .then((r) => (r.ok ? r.json() : null))
      .then(setStats)
      .catch(() => {});
  }, []);

  if (!stats) return null;
  return (
    <span className="tabular-nums">
      {stats.trials.toLocaleString()} trials · {stats.papers.toLocaleString()} papers · updated{" "}
      {UPDATED.format(new Date(stats.updated_at))}
    </span>
  );
}
