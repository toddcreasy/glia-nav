import type { NextConfig } from "next";

import { version } from "./package.json";

const nextConfig: NextConfig = {
  // Static export: the whole app is client-rendered, so Amplify serves plain files
  // and there is no SSR compute to pay for.
  output: "export",
  // The footer's release line. VERSION is package.json's, which a release bumps. RELEASED_AT is
  // when this build ran, in UTC; CI builds on merge and deploys the result, so on the public site
  // it is the release date.
  env: { VERSION: version, RELEASED_AT: new Date().toISOString() },
};

export default nextConfig;
