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
# memray publishes no musllinux aarch64 wheel for any version, so on Apple
# Silicon uv has to build its sdist — and `main` already deleted build-base.
#
# The LDFLAGS are not optional. memray's sdist links its C++ extension through
# setuptools' LDSHARED (`cc -shared`, not the C++ driver), so libstdc++ never
# lands in DT_NEEDED; musl resolves relocations eagerly, so the import dies on
# `_ZSt20__throw_length_errorPKc` even though the library is right there.
# `--no-as-needed` is required too: LDFLAGS is appended ahead of the object
# files and Alpine's gcc would otherwise drop the unused-looking -lstdc++.
#
# Nothing is removed afterwards: libstdc++, libgcc, liblz4, libunwind,
# libucontext and libdebuginfod are all runtime deps of the built extension.
# This image is never deployed — production builds the `main` target.
RUN apk add --no-cache build-base pkgconf libunwind-dev lz4-dev elfutils-dev \
    && LDFLAGS="-Wl,--no-as-needed -lstdc++" uv sync --frozen --no-cache
COPY ./tests /code/tests
