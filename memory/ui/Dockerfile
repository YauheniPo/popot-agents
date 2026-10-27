# syntax=docker/dockerfile:1
# Native React extension of the same pinned Cognee release as the backend.
FROM node-base AS source
ARG COGNEE_COMMIT
ARG COGNEE_SOURCE_SHA256
ADD --checksum=sha256:${COGNEE_SOURCE_SHA256} https://codeload.github.com/topoteretes/cognee/tar.gz/${COGNEE_COMMIT} /tmp/cognee.tar.gz
RUN mkdir /source && tar -xzf /tmp/cognee.tar.gz --strip-components=1 -C /source

FROM node-base AS builder
WORKDIR /app
RUN apk add --no-cache libc6-compat python3
COPY --from=source /source/cognee-frontend/package*.json ./
RUN npm ci
COPY --from=source /source/cognee-frontend/ ./
COPY linkedKnowledge.ts KnowledgePanel.tsx knowledgeLabels.ts apply-overlay.py /overlay/
RUN python3 /overlay/apply-overlay.py /app
ENV NEXT_PUBLIC_IS_CLOUD_ENVIRONMENT=false NEXT_TELEMETRY_DISABLED=1
RUN npx eslint src/modules/business/linkedKnowledge.ts src/modules/business/KnowledgePanel.tsx src/modules/business/knowledgeLabels.ts src/modules/business/layers/contentLayer.ts src/modules/business/panels/NodePanel.tsx 'src/app/(app)/business/BusinessView.tsx'
RUN npm run build

FROM cognee-ui-base AS runner
LABEL com.popot.project="popot-agents"
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone /app/
COPY --from=builder --chown=nextjs:nodejs /app/.next/static /app/.next/static
USER nextjs
