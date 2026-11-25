"use client";

import { useEffect, useState } from "react";

import { Dot } from "./released";

type Stats = { trials: number; papers: number; updated_at: string };

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api";

// The corpus counts the daily ingest publishes, read live from /stats. Until it has, nothing shows.
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
    <>
      <Dot />
      <span className="tabular-nums">{stats.trials.toLocaleString()} trials</span>
      <Dot />
      <span className="tabular-nums">{stats.papers.toLocaleString()} papers</span>
    </>
  );
}
