"""The release's version and the User-Agent every request of AIOSubtitles carries, in one place.

The version is the VERSION file beside subsync's folder (the repository's root; /opt/aiosubtitles in the image), read
once: both addon manifests and every User-Agent take it from here, so a release changes one file.
"""
from pathlib import Path

VERSION = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
PROJECT = "https://github.com/haslanli/AIOSubtitles"


def user_agent(part):
    """The User-Agent of `part`: "subsync" (the service's requests, its file reads too), "public" (the sign-up's) or
    "setup". It starts with "AIO": stock AIOStreams adds the files' details (streamData) to stream listings only for
    such clients (unless its PROVIDE_STREAM_DATA setting says otherwise), and without them no listed file can be read.
    It names the project's address, the contact Wikidata's terms ask for."""
    return f"AIOSubtitles/{VERSION} ({part}; +{PROJECT})"
