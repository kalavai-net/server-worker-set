# Use a lightweight Python image
FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/kalavai-net/server-worker-set

# Set the working directory
WORKDIR /app

# Install system dependencies (if needed for libraries like requests)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Kopf and the Kubernetes client
COPY . .

RUN pip install -r requirements.txt --no-cache-dir

# Run Kopf when the container starts
# --all-namespaces allows the operator to watch the whole cluster
CMD ["kopf", "run", "server_worker_set/operator.py", "--all-namespaces"]