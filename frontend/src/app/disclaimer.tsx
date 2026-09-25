/** Footer slot. The project-specific wording lands here in a later phase. */
export default function Disclaimer() {
  return (
    <footer className="border-t border-black/10 dark:border-white/15 px-6 py-4">
      <p className="text-xs text-black/60 dark:text-white/60">
        glia-nav is for research use only. It is not medical advice, and nothing here
        should be used to make treatment decisions. Talk to your care team.
      </p>
    </footer>
  );
}
