import IndexStats from "./index-stats";

const REPO = "https://github.com/toddcreasy/glia-nav";

export default function Disclaimer() {
  return (
    <footer className="border-t bg-card">
      <div className="mx-auto max-w-[1120px] space-y-2 px-6 py-4 text-[13px] text-muted-foreground">
        <p>
          GliaNav is for research use only. It is not medical advice, and nothing here should be
          used to make treatment decisions. Talk to your care team.
        </p>
        <p className="flex flex-wrap gap-x-3 gap-y-1">
          <IndexStats />
          <a
            href={`${REPO}/blob/main/LICENSE`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary underline-offset-4 hover:underline"
          >
            Apache 2.0 license
          </a>
          <a
            href={REPO}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary underline-offset-4 hover:underline"
          >
            Source on GitHub
          </a>
        </p>
      </div>
    </footer>
  );
}
