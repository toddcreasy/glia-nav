import type { NextConfig } from "next";

import { released, version } from "./package.json";

const nextConfig: NextConfig = {
  // Static export: the whole app is client-rendered, so Amplify serves plain files
  // and there is no SSR compute to pay for.
  output: "export",
  // The footer's release line, from package.json: a release sets `version` and `released` (its
  // date, YYYY-MM-DD) together. Not the build time, since every merge to main redeploys.
  env: { VERSION: version, RELEASED: released },
};

export default nextConfig;
