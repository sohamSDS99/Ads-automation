# ---------------------------------------------------------------------------
# web service — the only publicly reachable service.
# Railway: Root Directory = apps/web, dockerfilePath = Dockerfile (railway.json)
# ---------------------------------------------------------------------------
FROM node:24-alpine AS deps

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable

WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .

# --- build -----------------------------------------------------------------
FROM deps AS builder

# `next.config.ts` resolves `rewrites()` at BUILD time and bakes the result into
# `routes-manifest.json`; the standalone server never re-reads it. So the proxy
# target has to be present here, not merely at runtime — and Railway only passes
# a service variable into the build if the Dockerfile declares it as an `ARG`.
#
# Without this pair the build fell back to the compose default `http://api:8000`
# and shipped it to production, where that hostname does not resolve. Every
# /api/v1 call through the rewrite then failed with ENOTFOUND, which presents as
# a blank 500 page rather than anything that names the cause.
ARG API_INTERNAL_URL
ENV API_INTERNAL_URL=$API_INTERNAL_URL

RUN pnpm build

# --- run -------------------------------------------------------------------
FROM node:24-alpine AS runner

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

WORKDIR /app
RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

# `server.js` reads $PORT and $HOSTNAME. Bind `::` for Railway's IPv6-only
# private network; the public edge reaches this service the same way.
ENV HOSTNAME="::"
CMD ["node", "server.js"]
