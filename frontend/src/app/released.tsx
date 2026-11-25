"use client";

import { useSyncExternalStore } from "react";

// The release version and date (next.config.ts, from package.json). The date shows in the
// visitor's own locale, which only the browser knows; the static pages are built without it, so
// the date appears once the page runs in the browser.
const version = process.env.VERSION ?? "";
const released = process.env.RELEASED ?? "";
const noChange = () => () => {};

// The bold separator between the footer's items.
export const Dot = () => (
  <span aria-hidden="true" className="font-bold text-foreground">
    ·
  </span>
);

function local() {
  // A calendar date with no time: read and shown as UTC so no timezone moves it a day.
  const when = new Date(`${released}T00:00:00Z`);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
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
          <time dateTime={released}>{date}</time>
        </>
      )}
    </>
  );
}
