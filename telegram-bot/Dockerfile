FROM python:3.11-slim

WORKDIR /app

# Prevent Python from writing .pyc and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system runtime dependencies if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod 700 /app/docker-entrypoint.sh

# Expose notification receiver port (7879)
EXPOSE 7879

CMD ["python", "main.py"]
