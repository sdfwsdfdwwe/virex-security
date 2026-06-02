FROM python:3.13-slim

WORKDIR /app

# Install system dependencies for audio support
RUN apt-get update && apt-get install -y \
    libopus0 \
    libffi8 \
    openssl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Run the bot
CMD ["python", "main.py"]

