# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# subsync: a Stremio subtitles addon for an AIOStreams custom addon, and the worker that syncs subtitles.
# alass-cli 2.0.0 (GPL-3.0) is built from crates.io and run as a separate program; Silero VAD 6.2.3 (MIT) and PP-OCRv5
# line recognition (Apache-2.0) run on onnxruntime (no torch); ffmpeg (Debian) decodes the audio windows. The build
# stops unless the project's tests pass (including alass and ffmpeg ones). The first line pins the Dockerfile syntax
# (1.7, by digest) that ADD --checksum below needs, whatever BuildKit the machine's Docker has (Compose v2 always builds
# with BuildKit). Base images pinned by digest on 2026-11-09.
# Built from the repository's root (docker build -f subsync/Dockerfile .): the image also carries setup (installer/,
# VERSION), which runs inside it with `docker run... setup`, and Docker's command-line program and its Compose
# plugin, which setup uses through the Docker socket given to the setup container only (never to the service).
# An image built for another processor under emulation (QEMU runs these tests hours slower) can leave them out with
# --build-arg SUBSYNC_TESTS=skip; then run them on the machine the image is for, from the image, before using it.
# alass is compiled on the machine that builds, for the processor the image is for: an image for another processor
# (for example an x86_64 image built on an ARM machine) then needs no emulated compiler, which crashes under QEMU.
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


# Docker's command-line program from Docker's official image docker:29.9.3-cli, pinned by digest (Docker publishes no
# checksum for its static download), and its Compose plugin 5.7.1 from Docker's own release, pinned by SHA-256 per
# architecture (the release's checksums, equal to GitHub's own digests of the files on 2026-11-14). Only the two
# programs are copied into the image; nothing in these stages runs. A build without --platform (./setup's own) has no
# TARGETARCH, so the image's own architecture (dpkg) picks its Compose below, from a step that mounts both.
FROM docker:29.9.3-cli@sha256:5a7af0dcb0e46c803efe9b45371dd21cb500e5b4453250394ac6f06d49b6e6f1 AS dockercli
FROM scratch AS compose-amd64
ADD --chmod=755 --checksum=sha256:af426722a8af5de812fcee30cc1c39f0639f5f723cbadefb25e5d4db054064cf https://github.com/docker/compose/releases/download/v5.7.1/docker-compose-linux-x86_64 /docker-compose
FROM scratch AS compose-arm64
ADD --chmod=755 --checksum=sha256:f2f49f903240895fa764c0dca3a14548b754ad9426f6b609ccaf55ec5a3aa2a5 https://github.com/docker/compose/releases/download/v5.7.1/docker-compose-linux-aarch64 /docker-compose
FROM python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY subsync/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=alass /out/bin/alass-cli /usr/local/bin/alass
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
RUN --mount=type=bind,from=compose-amd64,target=/compose/amd64 --mount=type=bind,from=compose-arm64,target=/compose/arm64 \
    install -D -m 755 "/compose/$(dpkg --print-architecture)/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose
WORKDIR /opt/aiosubtitles/subsync
# onnxruntime (Silero VAD, the OCR models) sends usage events to Microsoft unless this is set before it loads;
# disable_telemetry_events alone doesn't stop its connection. Set before the tests run, too.
ENV ORT_DISABLE_TELEMETRY=1
COPY subsync/subsync ./subsync
COPY VERSION /opt/aiosubtitles/VERSION
COPY installer/*.py /opt/aiosubtitles/installer/
COPY --chmod=755 installer/image-entry.sh /opt/aiosubtitles/entry
# The pages' two typefaces, Atkinson Hyperlegible Next and Mono (SIL Open Font Licence 1.1, its text beside them),
# served by subsync itself so the pages load nothing from elsewhere; fetched from github.com/google/fonts at a pinned
# revision, the build stops unless each file has its pinned SHA-256. The folder is made first: a folder ADD makes takes
# the files' mode, 644, and the service's user then can't open it (a test instance answered the fonts with 404)
RUN mkdir -m 755 ./subsync/static/fonts
ADD --chmod=644 --checksum=sha256:5a455d1cfa099b601ab70751bb9673e8fe1854dc4500c80e1a220d0d75e31745 \
    https://raw.githubusercontent.com/google/fonts/35e77c3854970a550d9894a85aba21420813a019/ofl/atkinsonhyperlegiblenext/AtkinsonHyperlegibleNext%5Bwght%5D.ttf \
    ./subsync/static/fonts/AtkinsonHyperlegibleNext-wght.ttf
ADD --chmod=644 --checksum=sha256:5ce8b1698d1ded7dff2178c1a3ad159470085a58ea239e8b2cb88f4fb4a6f646 \
    https://raw.githubusercontent.com/google/fonts/35e77c3854970a550d9894a85aba21420813a019/ofl/atkinsonhyperlegiblemono/AtkinsonHyperlegibleMono%5Bwght%5D.ttf \
    ./subsync/static/fonts/AtkinsonHyperlegibleMono-wght.ttf
ADD --chmod=644 --checksum=sha256:aca6a428580965d2297d1b718042dd427c2a9443ece3b0d02d758e161e0c4030 \
    https://raw.githubusercontent.com/google/fonts/35e77c3854970a550d9894a85aba21420813a019/ofl/atkinsonhyperlegiblenext/OFL.txt \
    ./subsync/static/fonts/OFL.txt
# PP-OCRv5 mobile line recognition (PaddlePaddle, Apache-2.0), Latin and Eastern Slavic: reads image subtitle
# lines' words (imagetext.py); fetched from PaddlePaddle's own publication at pinned revisions, the build stops unless
# each file has its pinned SHA-256; their character lists are in subsync/ocr/
ADD --chmod=644 --checksum=sha256:7888113072263cb471b93f66dd5e2ad70548dc526fa1ace760d0d973dd121498 \
    https://huggingface.co/PaddlePaddle/latin_PP-OCRv5_mobile_rec_onnx/resolve/89d3a50e2c27e2e7cceeab0e944c25c807d5db4f/inference.onnx \
    ./subsync/ocr/latin_rec.onnx
ADD --chmod=644 --checksum=sha256:b3018ef2b09a0250b6e0c8e871c927098363e5fd4df890cc68e8358eb0aaf1bd \
    https://huggingface.co/PaddlePaddle/eslav_PP-OCRv5_mobile_rec_onnx/resolve/9a32171fc5718746875e1a261818884517975013/inference.onnx \
    ./subsync/ocr/eslav_rec.onnx
# Both test suites run from a read-only view of the build context (they never become a layer), each in a temporary
# folder of its own that is removed in the same step: nothing a test made stays in the image's /tmp.
ARG SUBSYNC_TESTS=run
RUN --mount=type=bind,source=.,target=/src \
    set -e; if [ "$SUBSYNC_TESTS" = skip ]; then echo "tests left out of this build: run them on the machine the image is for"; \
    else t=$(mktemp -d); export PYTHONDONTWRITEBYTECODE=1; \
      for suite in subsync installer; do \
        if [ "$suite" = subsync ]; then set -- -s /src/subsync/tests -t /src/subsync/tests; \
        else set -- -s /src/installer/tests -t /src; fi; \
        rc=0; TMPDIR="$t" AIOSUBTITLES_IMAGE_BUILD=1 python -m unittest discover "$@" -v > "$t/$suite.log" 2>&1 || rc=$?; \
        if [ "$rc" -ne 0 ]; then tail -60 "$t/$suite.log"; rm -rf "$t"; exit "$rc"; fi; tail -5 "$t/$suite.log"; \
      done; rm -rf "$t"; fi \
 && test -z "$(ls -A /tmp)" \
 && find /opt/aiosubtitles -name __pycache__ -prune -exec rm -rf {} + \
 && groupadd --system --gid 10001 subsync && useradd --system --uid 10001 --gid 10001 --no-create-home subsync \
 && mkdir /data && chown 10001:10001 /data \
 && setpriv --reuid=10001 --regid=10001 --clear-groups -- python -c "from subsync import ui; [ui.font_bytes(ui.font_path(n)) for n in ui.FONT_FILES]"
# Root by default, so `docker run ... setup` can use the socket; the entry script runs the service as 10001, never as
# root (compose files set user: "10001:10001" as well).
ENV SUBSYNC_DATA=/data
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=5)"
ENTRYPOINT ["/opt/aiosubtitles/entry"]
CMD ["python", "-u", "-m", "subsync"]
