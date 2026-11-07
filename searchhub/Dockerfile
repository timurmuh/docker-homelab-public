# searchhub: one FastAPI app behind uvicorn, built from this directory.
#
# Base pinned by digest — the same manifest open-webui/oikb/Dockerfile builds
# on: `python:3.13-slim` (Debian trixie), manifest list pushed 2026-10-28.
# Moving the digest is a deliberate act, not a side effect of a rebuild: read the
# new digest from Docker Hub, change it here, rebuild, and run the tests.
FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb

# No .pyc files in a read-only root FS (they could not be written anyway), and
# unbuffered output so `docker logs` is usable and the healthcheck's failure
# reasons are not stuck in a buffer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies before code: they change far less often, so edits to app/ do not
# re-run pip.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Numeric uid and no login shell: this process serves HTTP and needs neither a
# home directory nor a shell. A fixed uid keeps ownership predictable if a
# writable mount is ever added. Before the COPY below, so an app edit does not
# rebuild this layer.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser

COPY app /app/app

USER appuser

EXPOSE 8080

# The factory, not a module-level app: importing app.main must not require
# SEARCHHUB_API_KEY (Task 2's decision — the tests import it without one), so the
# key is read when uvicorn calls the factory. A missing key then fails the start
# instead of the import.
CMD ["uvicorn", "app.main:create_production_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
