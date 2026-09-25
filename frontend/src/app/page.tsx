"use client";

import { useState } from "react";
import Chat from "./chat";
import Search from "./search";

type Tab = "search" | "ask";

export default function Page() {
  const [tab, setTab] = useState<Tab>("search");

  // A public demo: no sign-in. The API bounds chat spend with a daily token limit.
  return (
    <div className="mx-auto max-w-3xl px-6 py-10 space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">glia-nav</h1>
        <p className="text-sm text-black/60 dark:text-white/60">
          Search glioblastoma clinical trials and research papers, or ask the
          navigator. Chat is a demo with a daily limit.
        </p>
      </header>

      <nav className="flex gap-4 border-b border-black/10 dark:border-white/15 text-sm">
        {(
          [
            ["search", "Search"],
            ["ask", "Ask the navigator"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            aria-pressed={tab === id}
            className={`-mb-px border-b-2 px-1 py-2 ${
              tab === id
                ? "border-foreground font-medium"
                : "border-transparent text-black/60 dark:text-white/60"
            }`}
          >
            {label}
          </button>
        ))}
      </nav>

      {/* Both stay mounted so switching tabs keeps results and the conversation. */}
      <div hidden={tab !== "search"}>
        <Search />
      </div>
      <div hidden={tab !== "ask"}>
        <Chat active={tab === "ask"} />
      </div>
    </div>
  );
}
