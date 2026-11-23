# Notices

AIOSubtitles (the installer in `installer/` and the subsync service in `subsync/`) is licensed AGPL-3.0-or-later
(`LICENSE`).

subsync's Docker image includes these third-party parts, unmodified:

| Part | Licence | How it is used |
|---|---|---|
| alass-cli 2.0.0 | GPL-3.0 | built from crates.io during the image build; run as a separate program |
| Silero VAD 6.2.3 | MIT | only its ONNX model file (the Python package is not imported) |
| PP-OCRv5 mobile line recognition, Latin and Eastern Slavic (PaddlePaddle) | Apache-2.0 | the ONNX models, fetched at build at pinned revisions and checked by SHA-256, and their character lists (`subsync/subsync/ocr/`): read the words of image subtitle lines |
| Atkinson Hyperlegible Next and Atkinson Hyperlegible Mono (The Atkinson Hyperlegible Next Project Authors) | SIL Open Font Licence 1.1 | the public pages' two typefaces, fetched at build from github.com/google/fonts at a pinned revision and checked by SHA-256, served by subsync itself with the licence's text beside them (`static/fonts/OFL.txt`) |
| onnxruntime 1.31.1 | MIT | runs the VAD model (audio route only) and the line recognition |
| numpy 2.5.4 | BSD-3-Clause | |
| cryptography 50.0.2 | Apache-2.0 or BSD-3-Clause | public mode only: encrypts each person's record |
| cffi 2.1.1 | MIT | needed by cryptography |
| pycparser 3.12 | BSD-3-Clause | needed by cffi |
| FFmpeg (Debian package) | LGPL-2.1+/GPL-2.0+ | decodes short audio windows (audio route only) |
| Docker CLI 29.9.3 (Docker, Inc.) | Apache-2.0 | from Docker's official image docker:29.9.3-cli, pinned by digest; used by setup only, never by the service |
| Docker Compose 5.7.1 (Docker, Inc.) | Apache-2.0 | Docker's release, checked by SHA-256; used by setup only |
| Python 3.13 base image, with its Debian packages | PSF; the Debian packages under their own licences | |

The image carries this file and these licences' texts in /opt/aiosubtitles/licences/.

TMDB's "alt short" logo (themoviedb.org/about/logos-attribution) is drawn in the pages where TMDB is used, with TMDB's
notice, as TMDB's API terms ask: AIOSubtitles uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise
approved by TMDB. Titles' original languages also come from Wikidata (CC0) and, for anime, AniList and Kitsu; only a
title's id is sent to them.

AIOStreams is a separate project (viren070/AIOStreams); subsync talks to it over HTTP and doesn't include any of it.
Subtitles come from the sources in your own AIOStreams config (for example OpenSubtitles) and from the sites
AIOSubtitles searches itself (OpenSubtitles, TheSubtitleDB and Gestdown; with their keys, SubDL, SubSource and Wyzie
Subs), under their own terms.
