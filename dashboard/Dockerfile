FROM python:3.11-alpine@sha256:25976e9d34a0fab1f278cae931f34c8303d97bf0c0d7f85b6b4dcf641d7702a4

WORKDIR /app

# Install curl for health checks
RUN apk add --no-cache curl

# Install dependencies and keep build tooling above audited security floors.
COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir --upgrade "pip==26.1.2" "setuptools==83.0.0" \
    && pip install --no-cache-dir -r requirements.txt

# Copy src modules from parent project
COPY src/__init__.py ./src/
COPY src/crm/ ./src/crm/

# Copy application code
COPY dashboard/app/ ./dashboard/app/

# Copy migration tooling without running migrations during image startup
COPY migrations/ ./migrations/

# Create config directory (credentials will be mounted)
RUN mkdir -p config

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8000

# Health check (start-period allows time for CRM initialization)
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/up || exit 1

# Run the application
CMD ["uvicorn", "dashboard.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
