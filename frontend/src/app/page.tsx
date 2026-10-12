"use client";

import { Button } from "@/components/ui/button";
import Chat from "./chat";
import Search from "./search";
import GitHubMark from "./github-mark";
import ThemeToggle from "./theme-toggle";

const REPO = "https://github.com/toddcreasy/glia-nav";

export default function Page() {
  // A public demo: no sign-in. The API bounds chat spend with a daily token limit.
  return (
    <>
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-[1120px] flex-wrap items-center gap-x-6 gap-y-3 px-6 py-4">
          <div className="flex items-center gap-2.5">
            <svg
              width="28"
              height="28"
              viewBox="0 0 28 28"
              aria-hidden="true"
              className="text-primary"
            >
              <rect
                x="1"
                y="1"
                width="26"
                height="26"
                rx="6"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              />
              <path
                d="M8 18c2-6 4-9 6-9s4 3 6 9"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
              <circle cx="14" cy="9" r="2" fill="currentColor" />
            </svg>
            <div className="flex flex-col leading-tight">
              <h1 className="font-serif text-xl font-semibold tracking-tight">
                glia-nav
              </h1>
              <p className="text-[13px] text-muted-foreground">
                Glioblastoma trials and research, in plain language
              </p>
            </div>
          </div>

          <div className="ml-auto flex items-center gap-1">
            <a
              href="https://github.com/toddcreasy"
              target="_blank"
              rel="noopener noreferrer"
              className="mr-2 text-[13px] text-muted-foreground hover:text-foreground"
            >
              Built by <span className="font-medium text-foreground">Todd Creasy</span>
            </a>
            <Button asChild variant="ghost" size="icon" className="text-muted-foreground">
              <a
                href={REPO}
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Source code on GitHub"
                title="Source code on GitHub"
              >
                <GitHubMark />
              </a>
            </Button>
            <ThemeToggle />
          </div>
        </div>
      </header>

      <div className="mx-auto flex max-w-[1120px] flex-wrap items-start gap-7 px-6 py-8">
        <div className="min-w-0 flex-[999_1_560px]">
          <Search />
        </div>
        <div className="min-w-0 flex-[1_1_380px]">
          <Chat />
        </div>
      </div>
    </>
  );
}
