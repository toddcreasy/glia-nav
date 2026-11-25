"use client";

import { useSyncExternalStore } from "react";

// The version and build date (next.config.ts). The date shows in the visitor's own locale and
// timezone, which only the browser knows; the static pages are built without them, so the date
// appears once the page runs in the browser.
const version = process.env.VERSION ?? "";
const released = process.env.RELEASED_AT ?? "";
const noChange = () => () => {};

// The bold separator between the footer's items.
export const Dot = () => (
  <span aria-hidden="true" className="font-bold text-foreground">
    ·
  </span>
);

function local() {
  const when = new Date(released);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

export default function Released() {
  const date = useSyncExternalStore(noChange, local, () => "");
  return (
    <>
      {version && (
        <>
          <Dot />
          <span>Release Version {version}</span>
        </>
      )}
      {date && (
        <>
          <Dot />
          <time dateTime={released} title={`${released} (UTC)`}>
            {date}
          </time>
        </>
      )}
    </>
  );
}
