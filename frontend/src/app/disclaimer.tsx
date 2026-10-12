/** Footer slot. The project-specific wording lands here in a later phase. */
export default function Disclaimer() {
  return (
    <footer className="border-t bg-card">
      <p className="mx-auto max-w-[1120px] px-6 py-4 text-[13px] text-muted-foreground">
        glia-nav is for research use only. It is not medical advice, and nothing here
        should be used to make treatment decisions. Talk to your care team.
      </p>
    </footer>
  );
}
