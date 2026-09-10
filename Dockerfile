FROM python:3.12-slim

# Match pyproject.toml requires-python (<3.13). Python 3.14 + unconstrained chromadb
# resolves have produced Chroma persist-dir metadata errors against volumes built
# with other client versions.

# System deps for pymupdf4llm, lxml, sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
# Prefer CPU-only torch (PyPI Linux default is CUDA + nvidia-*); Ollama/GPU stay on the host.
ENV UV_TORCH_BACKEND=cpu
# 1) Install third-party deps first (cacheable across code-only edits).
RUN uv sync --frozen --no-dev --no-install-project

# 2) Copy project code and install just the local package.
COPY src ./src
COPY scripts ./scripts
COPY eval ./eval
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY config.yaml ./
COPY topics.yaml ./
COPY data/dictionary.json ./data/dictionary.json

# The "how the bot works" reveal.js deck, served at /slides/ by the web UI.
# `docs/` as a whole is a runtime corpus mount, but the deck ships in the image.
COPY docs/slides ./docs/slides

# Surface the git version into the runtime env so the About page can show a
# commit hash / release tag even though `.git/` is not copied into the image.
# Pass at build time, e.g.
#     docker compose build --build-arg STUDENT_BOT_VERSION=$(git rev-parse --short HEAD)
ARG STUDENT_BOT_VERSION=""
ENV STUDENT_BOT_VERSION=${STUDENT_BOT_VERSION}

# Models are NOT baked into the image. There used to be a build-time pre-warm
# here, and it earned its ~1.5 GB in neither of the ways it was meant to:
#
#   * it cached `intfloat/multilingual-e5-base`, while config.yaml has used
#     `BAAI/bge-m3` since the cross-lingual switch — so the embedding half of
#     the cache was for a model the app never loads;
#   * docker-compose.yml sets HF_HOME=/hfcache and mounts the shared `hf_cache`
#     volume there, so the baked /app/.hf_cache was shadowed at runtime and
#     never read in production at all.
#
# Dropping it cuts ~1.5 GB from the image and from every build-cache layer
# that carried it, which matters: build cache reached 11.2 GB on this host's
# 30 GB disk and failed a deploy mid layer-export.
#
# The models land in the `hf_cache` volume on first use and are then shared by
# both services. To warm it deliberately (e.g. before first start on a new
# host) rather than paying the download on a student's first question:
#
#     docker compose run --rm beta-web python -c "\
#     from sentence_transformers import SentenceTransformer, CrossEncoder; \
#     SentenceTransformer('BAAI/bge-m3', device='cpu'); \
#     CrossEncoder('cross-encoder/mmarco-mMiniLMv2-L12-H384-v1', device='cpu')"
#
# A bare `docker run` without the volume downloads into HF_HOME below.
ENV TRANSFORMERS_OFFLINE=0 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/app/.hf_cache

# Default: run the Mattermost bot. Override with `docker compose run --rm bot python -m scripts.reindex` etc.
CMD ["python", "-m", "student_bot.bot.mattermost_client"]
