FROM node:24-alpine AS build
ARG VITE_AUTH_REQUIRED=false
ARG VITE_KEYCLOAK_URL=http://localhost:8088
ARG VITE_KEYCLOAK_REALM=semora
ARG VITE_KEYCLOAK_CLIENT_ID=semora-web
ENV VITE_AUTH_REQUIRED=$VITE_AUTH_REQUIRED \
    VITE_KEYCLOAK_URL=$VITE_KEYCLOAK_URL \
    VITE_KEYCLOAK_REALM=$VITE_KEYCLOAK_REALM \
    VITE_KEYCLOAK_CLIENT_ID=$VITE_KEYCLOAK_CLIENT_ID
WORKDIR /app
COPY apps/web/package*.json ./
RUN npm install
COPY apps/web/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY apps/web/nginx-main.conf /etc/nginx/nginx.conf
COPY apps/web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
RUN chown -R nginx:nginx /var/cache/nginx /var/run /usr/share/nginx/html
USER nginx
EXPOSE 8080

