# ── build ────────────────────────────────────────────────────────────────
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
# Vite bakes VITE_* vars into the bundle at build time. The model page links the
# browser at this MLflow origin; passed as a build arg from docker-compose.
ARG VITE_MLFLOW_URL
ENV VITE_MLFLOW_URL=${VITE_MLFLOW_URL}
# Login mode (local | none) from AUTH_MODE in manifests/.env; no default.
ARG VITE_AUTH_MODE
ENV VITE_AUTH_MODE=${VITE_AUTH_MODE}
RUN npm run build

# ── development target ───────────────────────────────────────────────────
# Used by docker-compose.dev.yml when you want to reproduce the dev stack in
# containers. For day-to-day dev, Tilt runs `npm run dev` on the host instead.
# node:20-alpine ships with a built-in `node` user (UID 1000); drop privileges.
FROM node:20-alpine AS development
WORKDIR /app
COPY --chown=node:node package.json .
RUN npm install
# Vite needs vite.config.ts, index.html, tsconfig.json, public/, etc. at the
# project root — not just src/ — to bootstrap. .dockerignore drops node_modules.
COPY --chown=node:node . .
USER node
EXPOSE 5173

# ── production ───────────────────────────────────────────────────────────
# Rootless nginx (UID 101) listens on 8080. Doc root is /usr/share/nginx/html.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS production
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
