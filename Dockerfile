# Use the official Microsoft Playwright image with Python 3.11
# This image already contains all the dependencies for Chromium/Chrome
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PORT 8000
ENV TZ=America/Manaus

# Set working directory
WORKDIR /app

# Install system dependencies and Google Chrome
RUN apt-get update && apt-get install -y wget \
    && wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt install -y ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (in case they are not in the image)
RUN playwright install chromium

# Copy project files
COPY . .

# Create tokens directory if it doesn't exist
RUN mkdir -p tokens

# Start the application - Usando 1 worker fixo (uvicorn) para evitar conflitos de conexão no WhatsApp
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
