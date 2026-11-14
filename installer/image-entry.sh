#!/bin/sh
# The AIOSubtitles image's start. "setup ..." runs setup as root: Docker's socket needs it, and only the short-lived
# setup container is ever given the socket. Anything else (the service: python -u -m subsync) never runs as root:
# started as root, it switches to its own user (10001) first, and stops if it can't.
set -e
if [ "$1" = setup ]; then
  shift
  export AIOSUBTITLES_STATE=/folder/state AIOSUBTITLES_IN_IMAGE=1
  exec python -I -u /opt/aiosubtitles/installer/wizard.py "$@"   # -u: lines show at once, terminal or not
fi
if [ "$(id -u)" = 0 ]; then
  if ! setpriv --reuid=10001 --regid=10001 --clear-groups -- true 2>/dev/null; then
    echo "AIOSubtitles doesn't run as root, and this container can't switch to its own user (10001):" >&2
    echo "start it with --user 10001:10001 (in a compose file: user: \"10001:10001\")." >&2
    exit 1
  fi
  exec setpriv --reuid=10001 --regid=10001 --clear-groups -- "$@"
fi
exec "$@"
