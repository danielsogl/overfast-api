# Build arguments
ARG PYTHON_VERSION=3.14
ARG UV_VERSION=0.12.7

# Create a temporary stage to pull the uv binary
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-stage

# Main stage
FROM python:${PYTHON_VERSION}-alpine AS main

# Copy the uv binary from the temporary stage to the main stage
COPY --from=uv-stage /uv /bin/uv

# Copy only requirements (caching in Docker layer)
COPY pyproject.toml uv.lock /code/

# Sync the project into a new environment (no dev dependencies)
WORKDIR /code

# Install the project
RUN apk add --no-cache build-base \
    && uv sync --frozen --no-cache --no-dev \
    && apk del build-base

# Copy code and static folders
COPY ./app /code/app
COPY ./static /code/static

# For dev image, copy the tests and install necessary dependencies
FROM main AS dev
RUN uv sync --frozen --no-cache
# `tests/scripts/` imports `scripts.` directly, so the helper scripts are part
# of the dev image's test surface. CI runs pytest on the runner where the
# whole repo is present, so a missing copy here only ever broke the Docker
# path — as a collection ImportError naming the test, not the layout.
COPY ./tests /code/tests
COPY ./scripts /code/scripts
