#!/bin/bash
set -e

# Run database migrations before starting the application.
# This ensures the schema is up-to-date on every deploy/restart,
# eliminating race conditions between the migrate service and backend.
echo "Running Alembic migrations..."
python -m alembic upgrade head
echo "Migrations complete."

# Execute the CMD (defaults to uvicorn)
exec "$@"
