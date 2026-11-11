ARG PROXY_BASE_IMAGE=python:3.13-slim-bullseye
FROM ${PROXY_BASE_IMAGE}

# Supply an official release binary after verifying its published SHA-256.
COPY --chmod=0555 mihomo /usr/local/bin/mihomo
COPY --chmod=0555 healthcheck.py /usr/local/bin/groq-egress-healthcheck.py
USER 65532:65532
HEALTHCHECK --interval=180s --timeout=15s --start-period=20s --retries=3 \
    CMD ["python", "/usr/local/bin/groq-egress-healthcheck.py"]
ENTRYPOINT ["/usr/local/bin/mihomo"]
CMD ["-d", "/tmp/mihomo", "-f", "/etc/mihomo/config.json"]
