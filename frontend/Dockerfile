FROM node:18-alpine AS builder

WORKDIR /app

# Install dependencies
COPY package.json package-lock.json ./
RUN npm ci

# Copy source and build
COPY . .
RUN npm run build

# Production stage — serve with nginx
FROM nginx:alpine

# Build arg for the backend upstream host.
# Local dev (docker-compose):  backend:8000
# Azure Container Apps:        atis-backend.internal.<env>.azurecontainerapps.io
ARG BACKEND_HOST=backend:8000

# Copy built React files
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx config, substitute BACKEND_HOST_PLACEHOLDER at build time with sed.
# Using a placeholder avoids envsubst clobbering nginx's own $variables at runtime.
COPY nginx.conf /etc/nginx/conf.d/default.conf
RUN sed -i "s|BACKEND_HOST_PLACEHOLDER|${BACKEND_HOST}|g" /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
