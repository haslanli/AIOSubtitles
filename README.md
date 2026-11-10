# AIOSubtitles: synced subtitles for AIOStreams

> **Status, November 2026: partly repaired.** Synced copies now work for files that have their own subtitle track
> (tested on a stock AIOStreams + AIOMetadata setup: two films and an episode, in English and Russian). Not fixed yet:
> the audio option, for files with no subtitle track of their own, has not produced a synced copy in testing, so leave
> it off; and the wizard's closing test read can fail, which does not affect the install (answer no to skip it).

**subsync** makes copies of your subtitles timed to the exact file you are playing, and lists them next to the originals
as **"English (synced)"** (or any language you pick). It checks every copy against the file's own subtitle track, so a
copy that doesn't fit is never offered: you get the originals as before.

`./setup` adds subsync to the AIOStreams you already run. It installs nothing else.

## What you need

- A machine running **AIOStreams** (v2.37 or newer), usually in Docker. subsync goes on the same machine.
- **Docker** with Docker Compose v2, **Python 3.9+**, and about **3 GB** of free disk space.
- An AIOStreams config with a **debrid service** (TorBox, Real-Debrid, ...). subsync reads debrid files only.

## Install

On the machine that runs AIOStreams:

```bash
git clone https://github.com/haslanli/AIOSubtitles.git
```

```bash
cd AIOSubtitles
```

```bash
./setup
```

It takes about 2 minutes of questions, then about 10 minutes the first time while subsync builds. Each question has a
default: press Enter to accept it, or type `?` for more help.

**It asks:**

1. Where AIOStreams is. It looks on this machine first.
2. The ID and password of the config your apps use. You can paste your Stremio link instead of the ID. The password is
   used during setup and not stored.
3. Which languages to sync. The default is English.
4. Whether to also sync files that carry no subtitles of their own, by listening to the audio. The default is no.
5. Where your apps can reach subsync. The default is the same web address as AIOStreams, for example
   `https://aio.example.com/subsync`. It shows the one line to add to your web server.

## What it changes

It shows each change first and saves it only after you say yes.

- **Your config gets one more subtitle source, subsync.** A copy of the config as it was is saved in `state/backups/`
  first, and `./setup --restore FILE` puts it back. If the config has no subtitle source at all, it offers to add
  OpenSubtitles too: Stremio's official addon, which needs no account. Nothing else in your config changes.
- **A second config for subsync.** It is made from yours, without Usenet sources and without subsync, and nothing runs
  in it in the background. subsync uses it to find the file you are watching. It never appears in your apps.
- **One Docker container, `aiosubtitles-subsync`.** It restarts by itself after a reboot. It joins AIOStreams' Docker
  network so the two can talk to each other.

It never changes AIOStreams' own server settings, with one optional exception: with AIOStreams' admin sign-in, it can
turn off AIOStreams' 5-minute subtitle cache for subsync only, so synced copies appear sooner. It never touches
AIOMetadata or any other app.

## What subsync reads

- **Every file you play:** about 1 MB, made up of the start of the file, its index, and 64 KB at each end. That is
  enough when the file has its own subtitle track, which most files do.
- **Files without their own subtitle track:** only with the audio option on, and only after you pick the
  "<Language> (Subsync)" entry for that file in your app: it plays the original subtitle at once, and subsync then
  reads audio from the file (hundreds of MB to a few GB, more on high-bitrate files) to make a synced copy for the
  next time you open it. Files you only browse are never read this way. With the option off, these files keep their
  original subtitles. MP4 files are supported as well as MKV.
- **Real-Debrid:** if your config sends Real-Debrid files straight to your apps (without AIOStreams' proxy),
  Real-Debrid sees your account used from two places at once. setup warns you about this and limits subsync to the
  1 MB reads.
- **Limits:** subsync reads one file at a time, stops for a while if a service refuses, and never reads anything you
  didn't open.

## What you will see

| App | What shows |
|---|---|
| **Stremio** | Playing a file starts subsync on it, which takes a few seconds. The synced copy shows the next time you open that file. Without the cache change, that can take up to 5 minutes. |
| **AIOStreams' Jellyfin server** | The same, named "(synced)". |
| **AIOMetadata**, stock | It keeps subtitle lists for up to an hour and shows a plain language name instead of "(synced)". It can rarely play the subtitle next to the one you picked. |

## Everyday commands

```bash
./setup
```

Change languages or options. Your answers are kept unless you change them.

```bash
./setup --status
```

Shows whether subsync is running and what it has done today.

```bash
./setup --uninstall
```

Takes subsync out of your config, deletes its own config and removes the container.

```bash
docker logs aiosubtitles-subsync
```

subsync's log. It contains no links and no keys.

To answer the questions from a file instead, use `./setup --answers FILE`, and add `--yes` to ask nothing. See
`docs/answers.example`.

## How the sync works

1. subsync reads the played file's index and its own subtitle track's timings.
2. It tries the subtitles you would see, best match for this release first.
3. Each one is re-timed with alass, then each line is snapped to the file's own line.
4. A copy is kept only if at least 90 % of its lines land within 200 ms of the file's track, spread over the whole
   film. Copies whose lines snapped need 95 %.
   A copy that misses that is still kept when it is clearly better than the original: its typical error is 200 ms or
   less and at most half of the original's, at least 85 % of its lines pair with the file's lines, and no part of
   the film is left out. A subtitle made by someone else rarely matches the file's own line starts more closely.
5. An original that already fits is listed as it is.

Files with only image subtitles (PGS, VobSub) use those subtitles' timings instead of a text track. Files with no
subtitle track use the audio route, if it is on.

## Folder

| Path | What it holds |
|---|---|
| `setup` | Starts the installer, `installer/wizard.py`. |
| `installer/` | The installer, written in plain Python with no packages to install. Its tests run with `python3 -m unittest discover -s installer/tests -t .`. |
| `subsync/` | The service. Its Docker build runs its own tests and stops if any test fails. |
| `state/` | Your settings, keys and config backups, readable only by you. Never share or commit this folder. |

## Licence

AGPL-3.0-or-later; see `LICENSE` and `NOTICE.md`.
