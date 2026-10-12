// PilotBio's mark from pilotbio.io. The plane takes the text color so it shows in dark mode.
export default function PilotBioMark() {
  return (
    <svg viewBox="0 0 36 42" aria-hidden="true" className="h-6 w-auto">
      <path
        d="M10 22 H18 C25 22 28 18 28 13.5 C28 9 25 5 18 5 H9 V28"
        fill="none"
        stroke="#C8102E"
        strokeWidth="3.2"
        strokeDasharray="3.4 2.6"
        strokeLinecap="round"
      />
      <path
        fill="currentColor"
        transform="translate(9 34) rotate(90) scale(0.8) translate(-12 -12)"
        d="M21.5 12c0-.8-.7-1.4-1.5-1.4h-5.2L10 2.5H8l2.4 8.1H5.6L3.8 8H2.3l1 4-1 4h1.5l1.8-2.6h4.8L8 21.5h2l4.8-8.1H20c.8 0 1.5-.6 1.5-1.4z"
      />
    </svg>
  );
}
