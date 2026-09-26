"use client";

import { Fragment, useEffect, useState, type FormEvent, type KeyboardEvent } from "react";

// Deployed, this is App Runner itself, not the same-origin /api rewrite: Amplify's
// proxy cuts requests off at 30 s and an agent turn can run longer.
const API_URL = process.env.NEXT_PUBLIC_CHAT_API_URL || process.env.NEXT_PUBLIC_API_URL || "/api";

// The API caps a message at this length.
const MAX_MESSAGE = 2000;

// NCT IDs and PMIDs in an answer become links to the source record.
const CITATION = /(NCT\d{8}|PMID:?\s*\d{5,9})/g;

type Turn = { role: "user" | "agent"; text: string };

// Today's chat allowance from the API's daily token limit.
type Usage = { limit: number; used: number; remaining: number; resets_at: string };

// The limit resets at midnight US Eastern, so the reset is shown in that zone.
const RESET_TIME = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
  timeZoneName: "short",
});

function citationLink(match: string) {
  if (match.startsWith("NCT")) {
    return `https://clinicaltrials.gov/study/${match}`;
  }
  return `https://pubmed.ncbi.nlm.nih.gov/${match.replace(/\D/g, "")}/`;
}

// Answers arrive as plain text with some Markdown emphasis; the asterisks are dropped
// rather than rendered, and citations are linked.
function AgentText({ text }: { text: string }) {
  const parts = text.replaceAll("**", "").split(CITATION);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <a
            key={i}
            href={citationLink(part)}
            target="_blank"
            rel="noopener noreferrer"
            className="underline underline-offset-4 hover:no-underline"
          >
            {part}
          </a>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        ),
      )}
    </>
  );
}

// `active` is whether the chat tab is showing. The allowance is fetched only then, so a
// visit that never opens chat does not wake the database.
export default function Chat({ active }: { active: boolean }) {
  const [conversationId, setConversationId] = useState(() => crypto.randomUUID());
  const [usage, setUsage] = useState<Usage | null>(null);
  // What the agent is doing on the turn in flight, streamed from /chat.
  const [steps, setSteps] = useState<string[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function loadUsage() {
    try {
      const response = await fetch(`${API_URL}/usage`);
      if (response.ok) setUsage(await response.json());
    } catch {
      // The line is informational; chat still works without it.
    }
  }

  useEffect(() => {
    if (!active || usage !== null) return;
    fetch(`${API_URL}/usage`)
      .then((response) => (response.ok ? response.json() : null))
      .then((body) => body && setUsage(body))
      .catch(() => {});
  }, [active, usage]);

  async function send(event?: FormEvent) {
    event?.preventDefault();
    const text = message.trim();
    if (!text || busy) return;
    setTurns((current) => [...current, { role: "user", text }]);
    setMessage("");
    setBusy(true);
    setError("");
    setSteps([]);

    try {
      const response = await fetch(`${API_URL}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversation_id: conversationId }),
      });
      // The demo's daily and hourly limits; the API says which and when it resets.
      if (response.status === 429) {
        setError((await response.json()).detail);
        loadUsage();
        return;
      }
      if (!response.ok || !response.body) {
        setError(`The navigator could not answer (${response.status}). Try again.`);
        return;
      }
      // Server-sent events: progress lines while the agent works, then one answer or error.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let finished = false;
      while (!finished) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() ?? "";
        for (const chunk of chunks) {
          const data = chunk.split("\n").find((line) => line.startsWith("data:"));
          if (!data) continue;
          const streamed = JSON.parse(data.slice(5));
          if (streamed.type === "progress") {
            setSteps((current) => [...current, streamed.text]);
          } else if (streamed.type === "answer") {
            setTurns((current) => [...current, { role: "agent", text: streamed.answer }]);
            setUsage(streamed.usage);
            finished = true;
          } else {
            setError("The navigator could not answer. Try again.");
            finished = true;
          }
        }
      }
      if (!finished) setError("The navigator stopped without answering. Try again.");
    } catch (err) {
      setError(`The navigator could not answer: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter adds a line.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }

  function newConversation() {
    setConversationId(crypto.randomUUID());
    setTurns([]);
    setError("");
  }

  return (
    <section className="space-y-4">
      <div className="flex items-baseline justify-between gap-4">
        <p className="text-sm text-black/60 dark:text-white/60">
          Ask about glioblastoma trials and research. Answers cite trials and papers from the
          search index.
        </p>
        {turns.length > 0 && (
          <button
            type="button"
            onClick={newConversation}
            disabled={busy}
            className="shrink-0 text-sm underline underline-offset-4 hover:no-underline disabled:opacity-50"
          >
            New conversation
          </button>
        )}
      </div>

      {usage && (
        <p className="text-xs text-black/60 dark:text-white/60">
          {usage.remaining.toLocaleString()} of {usage.limit.toLocaleString()} chat tokens left
          today. Resets {RESET_TIME.format(new Date(usage.resets_at))}.
        </p>
      )}

      <ol className="space-y-4">
        {turns.map((turn, i) => (
          <li
            key={i}
            className={
              turn.role === "user"
                ? "ml-auto max-w-[85%] rounded bg-black/5 dark:bg-white/10 px-3 py-2 whitespace-pre-wrap"
                : "max-w-[95%] whitespace-pre-wrap text-sm leading-relaxed"
            }
          >
            {turn.role === "agent" ? <AgentText text={turn.text} /> : turn.text}
          </li>
        ))}
      </ol>

      {busy && (
        <div className="space-y-1 text-sm text-black/60 dark:text-white/60">
          <p>
            Thinking… The first message after a quiet spell can take up to a minute while the
            database wakes.
          </p>
          {steps.length > 0 && (
            <ul className="space-y-1">
              {steps.map((step, i) => (
                <li key={i} className={i === steps.length - 1 ? "text-foreground" : undefined}>
                  {step}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {error && <p className="text-sm text-red-700 dark:text-red-400">{error}</p>}

      <form onSubmit={send} className="flex items-end gap-2">
        <textarea
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={onKeyDown}
          maxLength={MAX_MESSAGE}
          rows={2}
          placeholder="e.g. Which trials for recurrent glioblastoma are recruiting in Massachusetts?"
          aria-label="Message"
          className="flex-1 resize-y rounded border border-black/15 dark:border-white/20 bg-transparent px-3 py-2"
        />
        <button
          type="submit"
          disabled={busy || !message.trim()}
          className="rounded bg-foreground px-4 py-2 text-sm text-background disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </section>
  );
}
