import type { NextConfig } from "next";

/**
 * The browser only ever talks to `web`. Every /api/v1 call is proxied from here
 * to the API over the private network, which keeps the app same-origin in every
 * environment — so the session cookie stays first-party `SameSite=Lax` and no
 * absolute API URL is ever shipped to the client (PRD §5.2, §13.1).
 */
const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://api:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${apiInternalUrl}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
