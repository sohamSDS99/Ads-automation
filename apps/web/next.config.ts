import type { NextConfig } from "next";

/**
 * The browser only ever talks to `web`. Every /api/v1 call is proxied from here
 * to the API over the private network, which keeps the app same-origin in every
 * environment — so the session cookie stays first-party `SameSite=Lax` and no
 * absolute API URL is ever shipped to the client (PRD §5.2, §13.1).
 */
const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://api:8000";

const isProduction = process.env.NODE_ENV === "production";

/**
 * The document CSP (P8 security pass).
 *
 * Every directive here was measured against what this app actually loads, not
 * copied from a template — a policy that is wrong is worse than none, because
 * the first thing a team does with a CSP that breaks the page is delete it.
 *
 * - `script-src 'self' 'unsafe-inline'`: Next's App Router inlines its hydration
 *   payload and flight data as `<script>` blocks. A nonce would be stricter, but
 *   nonces have to be minted per response in middleware and threaded through
 *   every inline script Next emits; Next does not expose all of them. The honest
 *   position is `'unsafe-inline'` with a note, not a nonce that silently misses
 *   half the scripts and gets `'unsafe-inline'` added back as a "fallback".
 * - `'unsafe-eval'` in development only: the dev overlay and fast refresh need
 *   it. Production does not, so production does not get it.
 * - `style-src 'unsafe-inline'`: Tailwind v4 emits a critical inline block, and
 *   `next-themes` writes an inline style on the root to avoid a flash of the
 *   wrong theme.
 * - `connect-src 'self'`: the app is same-origin by construction. If a future
 *   change needs a third-party endpoint, this is the line that should stop it
 *   and prompt the conversation.
 * - `img-src 'self' data: blob:`: creative screenshots stream from our own
 *   origin through the API; `blob:` covers the chart exports recharts produces.
 * - `frame-ancestors 'none'`: this app is never framed. It carries a session
 *   cookie, so being framed is only ever someone else's idea.
 */
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isProduction ? "" : " 'unsafe-eval'"}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  ...(isProduction ? ["upgrade-insecure-requests"] : []),
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  // `strict-origin-when-cross-origin` rather than `no-referrer`: the app links
  // out to competitor landing pages and to Google Ads, and sending the origin
  // (never the path — a path here carries project and run ids) is the polite
  // amount without leaking which run someone was reading.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value:
      "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()",
  },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  // Not `Cross-Origin-Embedder-Policy: require-corp`: nothing here needs cross-
  // origin isolation, and turning it on breaks image loading for no gain.
  { key: "X-DNS-Prefetch-Control", value: "off" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // The framework's version is not a fact a visitor needs.
  poweredByHeader: false,
  async headers() {
    return [
      {
        // Documents and assets. `/api/v1/*` is excluded: those responses are
        // proxied from the API, which sets its own far stricter policy, and two
        // `Content-Security-Policy` headers on one response are intersected by
        // the browser — which would silently apply this document policy to the
        // API's `default-src 'none'` and make neither one say what it means.
        source: "/((?!api/v1).*)",
        headers: securityHeaders,
      },
    ];
  },
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
