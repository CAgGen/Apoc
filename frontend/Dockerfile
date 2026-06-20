# APoc frontend — Vite + React 19, built to static assets and served by nginx
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Baked at build time: where the browser reaches the backend (host-published port)
ARG VITE_API_BASE=http://localhost:8800
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
