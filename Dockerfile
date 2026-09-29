# Single image for both the API and the Celery worker.
#
# One image, two roles, rather than two images: sentence-transformers pulls in
# torch, so the runtime layer is multiple gigabytes. Two images would either
# duplicate that layer or leave the worker without its dependencies. The API is
# the default command and the worker overrides it:
#
#   docker run --rm -p 8000:8000 support-api
#   docker run --rm support-api celery -A app.core.workers worker ...
#
# Migrations are deliberately NOT run by the entrypoint. init_db() uses
# create_all(), which creates missing tables but never alters an existing one,
# so a real deployment needs `alembic upgrade head` - and running that on every
# replica boot is a race when more than one replica starts at once. Run it as a
# separate deploy step:
#
#   docker run --rm support-api alembic upgrade head

# 3.14 matches the local interpreter and the CI matrix; torch/faiss resolve
# cp314 wheels only against recent numpy, so an older base will not install.
FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1: OpenMP, required by torch and faiss-cpu at import time. Without it
# the process dies with a bare "libgomp.so.1: cannot open shared object file"
# rather than a useful ImportError. No compilers are needed: every dependency
# in requirements.txt ships a manylinux wheel.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copied before the source so the dependency layer is cached until
# requirements.txt itself changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Source, migrations and the knowledge-base documents. data/embeddings/ is a
# generated FAISS cache and is left out; it is rebuilt from data/docs on first
# use.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY data/docs/ ./data/docs/

# Run as an unprivileged user. The API holds the database credentials and the
# webhook secret, so a container escape should not land on root.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Uses the interpreter already in the image rather than installing curl for one
# health probe. The connection failure is caught rather than left to raise: a
# refused connection is the expected unhealthy case, and letting it propagate
# would print a traceback into the container log on every failed probe.
# /health is a static route - it answers without touching the database, so a
# failing probe means the process is unhealthy, not that PostgreSQL is briefly
# unreachable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request
try:
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)
except Exception:
    sys.exit(1)"]

# --pool=solo, not the default prefork: the worker imports the same modules as
# the API, so forking would copy a multi-gigabyte torch process per child. This
# worker dispatches one notification task at a time and does not need
# concurrency.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
