import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static export: the whole app is client-rendered, so Amplify serves plain files
  // and there is no SSR compute to pay for.
  output: "export",
};

export default nextConfig;
