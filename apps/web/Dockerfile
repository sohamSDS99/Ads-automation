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
