# FlightPortrait network API. Explicit COPY allowlist — no tests, no ops
# scripts, nothing that is not imported at runtime ends up in the image.
FROM python:3.12-slim

WORKDIR /srv/api

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic alembic
COPY app app
COPY refdata refdata

EXPOSE 8092
# A deployment typically runs migrations first; the bare image still
# starts a working (schema-less until migrated) service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092"]
