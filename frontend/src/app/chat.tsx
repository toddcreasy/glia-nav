"use client";

import { Fragment, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/button";

// Deployed, this is App Runner itself, not the same-origin /api rewrite: Amplify's
// proxy cuts requests off at 30 s and an agent turn can run longer.
const API_URL = process.env.NEXT_PUBLIC_CHAT_API_URL || process.env.NEXT_PUBLIC_API_URL || "/api";

// The API caps a message at this length.
const MAX_MESSAGE = 2000;

// NCT IDs and PMIDs in an answer become links to the source record.
const CITATION = /(NCT\d{8}|PMID:?\s*\d{5,9})/g;

// An agent turn keeps the steps it streamed, shown checked above its answer.
type Turn = { role: "user" | "agent"; text: string; steps?: string[] };

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
            className="text-primary underline underline-offset-4 hover:no-underline"
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

function citations(text: string) {
  return [...new Set(text.match(CITATION) ?? [])].map((match) =>
    match.startsWith("NCT")
      ? { label: `Trial ${match}`, href: citationLink(match) }
      : { label: `Paper PMID ${match.replace(/\D/g, "")}`, href: citationLink(match) },
  );
}

const EXAMPLES = [
  "Which trials for recurrent glioblastoma are recruiting in Massachusetts?",
  "What is the standard treatment for newly diagnosed glioblastoma?",
  "Are there trials for MGMT-unmethylated glioblastoma?",
];

function Check() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true" className="shrink-0 text-success">
      <path
        d="M3 8.5l3 3 7-7"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Steps({ steps, live }: { steps: string[]; live: boolean }) {
  return (
    <ol className="space-y-1.5 text-[13px] text-muted-foreground">
      {steps.map((step, i) => {
        const current = live && i === steps.length - 1;
        return (
          <li key={i} className={`flex items-center gap-2 ${current ? "text-foreground" : ""}`}>
            {current ? (
              <span
                aria-hidden="true"
                className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-primary border-t-transparent"
              />
            ) : (
              <Check />
            )}
            {step}
          </li>
        );
      })}
    </ol>
  );
}

// Always on screen beside search. The allowance is fetched when someone first focuses the
// box, not on load, so a visit that never asks anything does not wake the database.
export default function Chat() {
  const [conversationId, setConversationId] = useState(() => crypto.randomUUID());
  const [usage, setUsage] = useState<Usage | null>(null);
  // What the agent is doing on the turn in flight, streamed from /chat.
  const [steps, setSteps] = useState<string[]>([]);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const log = useRef<HTMLDivElement>(null);
  // The same steps, read when the answer arrives to keep them with that turn.
  const turnSteps = useRef<string[]>([]);

  // Scroll the panel, not the page, to the newest turn.
  useEffect(() => {
    if (log.current) log.current.scrollTop = log.current.scrollHeight;
  }, [turns, steps, busy]);

  async function loadUsage() {
    try {
      const response = await fetch(`${API_URL}/usage`);
      if (response.ok) setUsage(await response.json());
    } catch {
      // The line is informational; chat still works without it.
    }
  }

  async function send(event?: FormEvent, preset?: string) {
    event?.preventDefault();
    const text = (preset ?? message).trim();
    if (!text || busy) return;
    setTurns((current) => [...current, { role: "user", text }]);
    setMessage("");
    setBusy(true);
    setError("");
    setSteps([]);
    turnSteps.current = [];

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
            turnSteps.current = [...turnSteps.current, streamed.text];
            setSteps(turnSteps.current);
          } else if (streamed.type === "answer") {
            const done = turnSteps.current;
            setTurns((current) => [...current, { role: "agent", text: streamed.answer, steps: done }]);
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
    <section
      id="ask"
      aria-label="Ask the navigator"
      className="flex scroll-mt-6 flex-col rounded-2xl border bg-card lg:sticky lg:top-6 lg:max-h-[calc(100vh-3rem)]"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b px-5 py-4">
        <h2 className="font-serif text-[19px] font-semibold">Ask the navigator</h2>
        <div className="flex items-baseline gap-3 text-[13px] text-muted-foreground tabular-nums">
          {usage && (
            <span title={`Resets ${RESET_TIME.format(new Date(usage.resets_at))}`}>
              {usage.remaining.toLocaleString()} tokens left today
            </span>
          )}
          {turns.length > 0 && (
            <button
              type="button"
              onClick={newConversation}
              disabled={busy}
              className="text-primary underline-offset-4 hover:underline disabled:opacity-50"
            >
              New conversation
            </button>
          )}
        </div>
      </div>

      <div ref={log} className="flex min-h-48 flex-col gap-4 overflow-y-auto p-5">
        {turns.length === 0 && !busy && (
          <div className="space-y-3">
            <p className="text-[15px] text-muted-foreground">
              Ask about glioblastoma trials and research. Answers cite only trials and papers the
              navigator found in the index, and every citation is checked.
            </p>
            <div className="flex flex-col items-start gap-2">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => send(undefined, example)}
                  className="rounded-xl border border-input px-3 py-2 text-left text-sm hover:border-primary hover:bg-secondary"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, i) =>
          turn.role === "user" ? (
            <p
              key={i}
              className="max-w-[85%] self-end rounded-2xl rounded-br-sm bg-foreground px-3.5 py-2.5 whitespace-pre-wrap text-background"
            >
              {turn.text}
            </p>
          ) : (
            <div key={i} className="space-y-3">
              {turn.steps && turn.steps.length > 0 && <Steps steps={turn.steps} live={false} />}
              <p className="leading-relaxed whitespace-pre-wrap">
                <AgentText text={turn.text} />
              </p>
              {citations(turn.text).length > 0 && (
                <div className="flex flex-wrap gap-1.5 text-[13px]">
                  {citations(turn.text).map((c) => (
                    <a
                      key={c.label}
                      href={c.href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="rounded-full border border-secondary-foreground/20 bg-secondary px-2.5 py-1 text-secondary-foreground hover:border-primary"
                    >
                      {c.label}
                    </a>
                  ))}
                </div>
              )}
            </div>
          ),
        )}

        {busy && (
          <div className="space-y-2">
            {steps.length > 0 ? (
              <Steps steps={steps} live />
            ) : (
              <p className="text-[13px] text-muted-foreground">
                Starting. The first question after a quiet spell can take up to a minute while
                the database wakes.
              </p>
            )}
          </div>
        )}
        {error && <p className="text-sm text-destructive">{error}</p>}
      </div>

      <form onSubmit={send} className="mt-auto flex items-end gap-2 border-t px-5 py-4">
        <label htmlFor="message" className="sr-only">
          Your question
        </label>
        <textarea
          id="message"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={onKeyDown}
          onFocus={() => usage === null && loadUsage()}
          maxLength={MAX_MESSAGE}
          rows={1}
          placeholder="Ask about trials or papers"
          className="min-h-11 flex-1 resize-y rounded-xl border border-input bg-card px-3.5 py-2.5 outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
        />
        <Button
          type="submit"
          disabled={busy || !message.trim()}
          className="h-11 rounded-xl px-4 font-semibold"
        >
          Send
        </Button>
      </form>
    </section>
  );
}
