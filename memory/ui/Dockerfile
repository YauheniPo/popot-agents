# syntax=docker/dockerfile:1
# Native React extension of the same pinned Cognee release as the backend.
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS source
ADD --checksum=sha256:167b0866ed0f019bccea5a655544c400845b5d2878af622452189b3bd890860b https://codeload.github.com/topoteretes/cognee/tar.gz/b32d8afc59e1064d9291b9828a8a147be9cc8bab /tmp/cognee.tar.gz
RUN mkdir /source && tar -xzf /tmp/cognee.tar.gz --strip-components=1 -C /source

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS builder
WORKDIR /app
RUN apk add --no-cache libc6-compat python3
COPY --from=source /source/cognee-frontend/package*.json ./
RUN npm ci
COPY --from=source /source/cognee-frontend/ ./
COPY linkedKnowledge.ts KnowledgePanel.tsx apply-overlay.py /overlay/
RUN python3 /overlay/apply-overlay.py /app
ENV NEXT_PUBLIC_IS_CLOUD_ENVIRONMENT=false NEXT_TELEMETRY_DISABLED=1
RUN npx eslint src/modules/business/linkedKnowledge.ts src/modules/business/KnowledgePanel.tsx src/modules/business/panels/NodePanel.tsx 'src/app/(app)/business/BusinessView.tsx'
RUN npm run build

FROM cognee/cognee-ui:main-b32d8af AS runner
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone /app/
COPY --from=builder --chown=nextjs:nodejs /app/.next/static /app/.next/static
