FROM python:3.12.13-slim AS builder

ARG UV_VERSION=0.11.23
WORKDIR /build
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv build --wheel

FROM python:3.12.13-slim AS runtime

ARG VERSION=2.1.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Bitcast X v3" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="https://github.com/bitcast-network/bitcast-x"

RUN groupadd --gid 10001 bitcast \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/bitcast bitcast \
    && mkdir -p /var/lib/bitcast-x /var/lib/bitcast-wallets \
    && chown -R bitcast:bitcast /var/lib/bitcast-x /var/lib/bitcast-wallets /home/bitcast
COPY --from=builder /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
COPY --chown=10001:10001 entrypoint.sh /entrypoint.sh
RUN chmod 0555 /entrypoint.sh

ENV HOME=/home/bitcast \
    PYTHONUNBUFFERED=1 \
    BITCAST_X_SOURCE_REVISION="${REVISION}" \
    BITCAST_X_STATE_DIR=/var/lib/bitcast-x \
    BITCAST_X_WALLET_PATH=/var/lib/bitcast-wallets
VOLUME ["/var/lib/bitcast-x"]
EXPOSE 8095 8096
USER 10001:10001
ENTRYPOINT ["/entrypoint.sh"]
CMD ["--help"]
