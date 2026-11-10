# Notices

AIOSubtitles (the installer in `installer/` and the subsync service in `subsync/`) is licensed AGPL-3.0-or-later
(`LICENSE`).

subsync's Docker image includes these third-party parts, unmodified:

| Part | Licence | How it is used |
|---|---|---|
| alass-cli 2.0.0 | GPL-3.0 | built from crates.io during the image build; run as a separate program |
| Silero VAD 6.2.3 | MIT | only its ONNX model file (the Python package is not imported) |
| onnxruntime 1.31.1 | MIT | runs the VAD model (audio route only) |
| numpy 2.5.4 | BSD-3-Clause | |
| FFmpeg (Debian package) | LGPL-2.1+/GPL-2.0+ | decodes short audio windows (audio route only) |
| Python 3.13 base image | PSF | |

AIOStreams is a separate project (viren070/AIOStreams); subsync talks to it over HTTP and doesn't include any of it.
Subtitles come from the sources in your own AIOStreams config (for example OpenSubtitles) under their own terms.
