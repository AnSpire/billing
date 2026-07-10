# Toolchain image for the Catala case studies (case1/case2/case3).
#
# This image only carries the *toolchain* (opam switch "catala" with the
# Catala compiler/clerk, plus Python for pytest). Source code is not copied in
# here — docker-compose.yml bind-mounts the repo at runtime, so editing a
# .catala_en or .py file never requires an image rebuild. Only rebuild when
# this Dockerfile changes (e.g. bumping the Catala version).
#
# Based on python:3.14-slim (Debian trixie) rather than a generic Linux base
# so the Python version matches modern dev machines exactly: Catala's
# generated Python (circular `from . import` chains between the transpiled
# stdlib modules) has been observed to raise a spurious
# "partially initialized module" AttributeError on Python 3.12, but runs
# clean on 3.14 — so the container's Python must track the reference version,
# not just "some python3".
FROM python:3.14-slim

ENV DEBIAN_FRONTEND=noninteractive \
    OPAMROOT=/opt/opam \
    OPAMYES=1

# opam: package manager for OCaml (builds/installs the Catala compiler).
# m4/pkg-config/libgmp-dev/build-essential: needed to compile OCaml itself and
# native deps like zarith.
RUN apt-get update && apt-get install -y --no-install-recommends \
        opam \
        m4 \
        pkg-config \
        libgmp-dev \
        build-essential \
        ninja-build \
        ca-certificates \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

# --disable-sandboxing: opam's bwrap-based build sandbox doesn't work inside
# most container setups (nested user namespaces); safe to skip here since the
# whole build already happens inside an isolated container.
RUN opam init --bare --disable-sandboxing -a \
    && opam switch create catala 4.14.2 \
    && opam install -y --switch=catala catala.1.2.1 \
    && opam clean --switch=catala -a -c -s -r

RUN pip install --no-cache-dir pytest

ENV PATH="/opt/opam/catala/bin:${PATH}"

# Run as a non-root user matching the host UID/GID (default: 1000/1000, the
# standard first-user id on a single-user Linux box). Without this, files the
# container writes into the bind-mounted repo (_build/, __pycache__/, ...)
# end up owned by root on the host and break host-side builds/edits.
# Override at build time if your host UID differs:
#   docker compose build --build-arg UID=$(id -u) --build-arg GID=$(id -g)
ARG UID=1000
ARG GID=1000
RUN if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" dev; fi \
    && if ! getent passwd "${UID}" >/dev/null; then useradd -m -u "${UID}" -g "${GID}" -s /bin/bash dev; fi \
    && chown -R "${UID}:${GID}" /opt/opam

USER ${UID}:${GID}
WORKDIR /workspace
CMD ["sleep", "infinity"]
