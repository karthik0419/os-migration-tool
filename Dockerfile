# ---- Stage 1: Build the UI ----
FROM node:20-slim AS ui-builder
WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ---- Stage 2: Python server + built UI ----
FROM python:3.12-slim
WORKDIR /app

# Install server deps
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

# Copy server code + engine
COPY server/app.py server/runner.py /app/server/
COPY server/engine/ /app/server/engine/

# Copy built UI from stage 1
COPY --from=ui-builder /app/ui/dist /app/ui/dist

# Data dir for run history + logs
RUN mkdir -p /app/server/data/logs
VOLUME /app/server/data

ENV OSMT_HOST=0.0.0.0
ENV OSMT_PORT=8020
ENV OSMT_DATA_DIR=/app/server/data

EXPOSE 8020

WORKDIR /app/server
CMD ["python", "app.py"]
