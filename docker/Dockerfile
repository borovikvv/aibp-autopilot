FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Install as package
RUN pip install -e .

# Create data directory
RUN mkdir -p data reports/logs

# Default command
CMD ["python3", "-m", "aibp.cli", "smoke-test"]
