# subsync: a Stremio subtitles addon for an AIOStreams custom addon, and the worker that syncs subtitles.
# alass-cli 2.0.0 (GPL-3.0) is built from crates.io and run as a separate program; Silero VAD 6.2.3 (MIT) runs on
# onnxruntime (no torch); ffmpeg (Debian) decodes the audio windows. The build stops unless the project's tests pass
# (including alass and ffmpeg ones). Base images pinned by digest on 2026-11-09.
# An image built for another processor under emulation (QEMU runs these tests hours slower) can leave them out with
# --build-arg SUBSYNC_TESTS=skip; then run them on the machine the image is for, from the image, before using it.
# alass is compiled on the machine that builds, for the processor the image is for: an image for another processor
# (an x86_64 server built on an ARM machine) then needs no emulated compiler, which crashes under QEMU.
FROM --platform=$BUILDPLATFORM rust@sha256:f6c4a35d21dbb3c89d7a8d056c193e94fc09037f2b58dd858b09c39a47dd02d7 AS alass
ARG TARGETARCH
RUN set -e; arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) triple=x86_64-unknown-linux-gnu; gcc=x86_64-linux-gnu-gcc; pkg=gcc-x86-64-linux-gnu ;; \
      arm64) triple=aarch64-unknown-linux-gnu; gcc=aarch64-linux-gnu-gcc; pkg=gcc-aarch64-linux-gnu ;; \
      *) echo "no alass build for $arch"; exit 1 ;; \
    esac; \
    if [ "$arch" != "$(dpkg --print-architecture)" ]; then \
      apt-get update && apt-get install -y --no-install-recommends "$pkg" "libc6-dev-$arch-cross" \
      && rm -rf /var/lib/apt/lists/* && rustup target add "$triple"; \
      export "CARGO_TARGET_$(echo "$triple" | tr 'a-z-' 'A-Z_')_LINKER=$gcc" \
             "CC_$(echo "$triple" | tr - _)=$gcc" "AR_$(echo "$triple" | tr - _)=${gcc%gcc}ar"; \
    fi; \
    cargo install alass-cli --version 2.0.0 --locked --root /out --target "$triple"

FROM python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=alass /out/bin/alass-cli /usr/local/bin/alass
WORKDIR /opt/subsync
COPY subsync ./subsync
COPY tests /tmp/tests
ARG SUBSYNC_TESTS=run
RUN if [ "$SUBSYNC_TESTS" = skip ]; then echo "tests left out of this build: run them on the machine the image is for"; \
    else python -m unittest discover -s /tmp/tests -t /tmp/tests -v > /tmp/tests.log 2>&1; rc=$?; \
      if [ "$rc" -ne 0 ]; then tail -60 /tmp/tests.log; exit "$rc"; fi; tail -5 /tmp/tests.log; fi \
 && rm -rf /tmp/tests /tmp/tests.log && find /opt/subsync -name __pycache__ -prune -exec rm -rf {} + \
 && useradd --system --uid 10001 --no-create-home subsync && mkdir /data && chown subsync /data
USER subsync
ENV SUBSYNC_DATA=/data
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=5)"
CMD ["python", "-u", "-m", "subsync"]
