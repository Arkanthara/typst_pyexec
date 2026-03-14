FROM python:3.12-slim-bookworm AS binaries

ARG TYPST_VERSION=0.12.0
ARG TINYMIST_VERSION=0.14.10

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    mkdir -p /tmp/typst /tmp/tinymist /opt/bin; \
    curl -fsSL "https://github.com/typst/typst/releases/download/v${TYPST_VERSION}/typst-x86_64-unknown-linux-musl.tar.xz" \
      | tar -xJ -C /tmp/typst; \
    install -m 0755 "$(find /tmp/typst -type f -name typst | head -n 1)" /opt/bin/typst; \
    curl -fsSL "https://github.com/Myriad-Dreamin/tinymist/releases/download/v${TINYMIST_VERSION}/tinymist-x86_64-unknown-linux-musl.tar.gz" \
      | tar -xz -C /tmp/tinymist; \
    install -m 0755 "$(find /tmp/tinymist -type f -name tinymist | head -n 1)" /opt/bin/tinymist

FROM python:3.12-slim-bookworm

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
  PIP_NO_CACHE_DIR=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

WORKDIR /workspace

COPY --from=binaries /opt/bin/typst /usr/local/bin/typst
COPY --from=binaries /opt/bin/tinymist /usr/local/bin/tinymist
COPY typst_ipy.py /usr/local/bin/typst_ipy.py

RUN chmod +x /usr/local/bin/typst_ipy.py \
  && python -m pip install --no-cache-dir ipykernel jupyter-client

# Useful for `tinymist preview -p` in devcontainers.
EXPOSE 23625

ENTRYPOINT ["python", "/usr/local/bin/typst_ipy.py"]
CMD ["--help"]
