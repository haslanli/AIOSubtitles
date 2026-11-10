# subsync: a Stremio subtitles addon for an AIOStreams custom addon, and the worker that syncs subtitles.
# alass-cli 2.0.0 (GPL-3.0) is built from crates.io and run as a separate program; Silero VAD 6.2.3 (MIT) runs on
# onnxruntime (no torch); ffmpeg (Debian) decodes the audio windows. The build stops unless the project's tests pass
# (including alass and ffmpeg ones). Base images pinned by digest on 2026-11-09.
FROM rust@sha256:f6c4a35d21dbb3c89d7a8d056c193e94fc09037f2b58dd858b09c39a47dd02d7 AS alass
RUN cargo install alass-cli --version 2.0.0 --locked --root /out

FROM python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=alass /out/bin/alass-cli /usr/local/bin/alass
WORKDIR /opt/subsync
COPY subsync ./subsync
COPY tests /tmp/tests
RUN python -m unittest discover -s /tmp/tests -t /tmp/tests -v 2>&1 | tail -5 && python -m unittest discover -s /tmp/tests -t /tmp/tests \
 && rm -rf /tmp/tests && find /opt/subsync -name __pycache__ -prune -exec rm -rf {} + \
 && useradd --system --uid 10001 --no-create-home subsync && mkdir /data && chown subsync /data
USER subsync
ENV SUBSYNC_DATA=/data
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=5)"
CMD ["python", "-u", "-m", "subsync"]
