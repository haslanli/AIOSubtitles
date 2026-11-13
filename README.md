# AIOSubtitles: synced subtitles for AIOStreams

> **Status, November 2026: being repaired.** Tested on a stock AIOStreams + AIOMetadata setup with films and episodes
> in English and Russian: synced copies work for files that have their own subtitle track (MKV and MP4), and each copy
> was checked against the file's own track. The audio option, for files without a subtitle track, has made its first
> synced copies in testing; treat it as new. On stock AIOMetadata a copy made while you watch shows up to an hour later
> (see "What you will see").

**subsync** makes copies of your subtitles timed to the exact file you are playing, and lists them next to the originals
as **"English (synced)"** (or any language you pick). It checks every copy against the file's own subtitle track, so a
copy that doesn't fit is never offered: you get the originals as before.

`./setup` adds subsync to the AIOStreams you already run. It installs nothing else.

## What you need

- A machine running **AIOStreams** (v2.37 or newer), usually in Docker. subsync goes on the same machine.
- **Docker** with Docker Compose v2, **Python 3.9+**, and about **3 GB** of free disk space.
- An AIOStreams config with a **debrid service other than Real-Debrid** (TorBox, AllDebrid, Premiumize, ...). subsync
  reads debrid files only, and never uses Real-Debrid (see [Fixed rules](#fixed-rules)).

## Install

On the machine that runs AIOStreams, or for a public instance, the machine its web address reaches:

```bash
git clone https://github.com/haslanli/AIOSubtitles.git
```

```bash
cd AIOSubtitles
```

```bash
./setup
```

Setup runs in your browser. `./setup` prints the address to open on this machine. On a server you reach over ssh, it
also prints one `ssh -L` line: run it on your own computer first, then open the same address there. The page stops
when you finish, after 60 minutes without use, or with Ctrl-C.

The first screen asks how you want to use it. **Install it for yourself**: this adds it to the AIOStreams you already
run. **Host a public instance**: runs here with its own sign-up page, so other people can use it with their own
AIOStreams. Each step says what it does and what it changes. Nothing in your AIOStreams changes until you confirm, and
a copy of your config is saved first. Every question, with its explanation, is also under [Settings](#settings). The
first build takes a few minutes (about 3 on a 4-core server).

To set it up without the page, give the answers in a file: `./setup --answers FILE --yes` (see
`docs/answers.example`).

## What it changes

It shows each change first and saves it only after you say yes.

- **Your config gets one more subtitle source, subsync.** A copy of the config as it was is saved in `state/backups/`
  first, and `./setup --restore FILE` puts it back. If the config has no subtitle source at all, it offers to add
  OpenSubtitles too: Stremio's official addon, which needs no account. Nothing else in your config changes.
- **A second config for subsync.** It is made from yours, without Usenet, without Real-Debrid and without subsync,
  and nothing runs in it in the background. subsync uses it to find the file you are watching. It never appears in your apps.
- **One Docker container, `aiosubtitles-subsync`.** It restarts by itself after a reboot. It joins AIOStreams' Docker
  network so the two can talk to each other.

It never changes AIOStreams' own server settings, with one optional exception: with AIOStreams' admin sign-in, it can
turn off AIOStreams' 5-minute subtitle cache for subsync only, so synced copies appear sooner. It never touches
AIOMetadata or any other app.

## What subsync reads

subsync reads only files your apps ask subtitles for:

- **The file you play:** about 1 MB, made up of the start of the file, its index, and 64 KB at each end (up to 16 MB
  for a file with very many subtitle tracks). That is enough when the file has its own subtitle track, which most
  files do. MKV and MP4.
- **With AIOMetadata, also the first version listed for a title you open,** even if you then play another one: stock
  AIOMetadata asks for the subtitles of the first listed version when it shows the versions. subsync reads only that
  file's index (about 1 MB), never its audio.
- **Files without their own subtitle track:** only with the audio option on, and only after you pick the
  "<Language> (Subsync)" entry for that file in your app: it plays the original subtitle at once, and subsync then
  reads short parts of the file's audio, at most 1 GB per file, to make a synced copy for the next time you open it.
  Files whose bitrate is too high for that (most 4K remuxes) keep their original subtitles. Files you only browse
  are never read this way. With the option off, these files keep their original subtitles.
- **Never Real-Debrid, never Usenet:** see [Fixed rules](#fixed-rules).
- **Limits:** subsync reads one file at a time, stops for a while if a service refuses, and has a daily limit (50 GB
  by default).

## What you will see

| App | What shows |
|---|---|
| **Stremio** | Opening or playing a file starts subsync on it, which takes a few seconds. Meanwhile a "<Language> (Subsync)" entry is listed: choosing it gives the synced copy once it is ready, else the original. From the next time you open that file the copy is listed as "<Language> (synced)"; without the cache change, that can take up to 5 minutes. |
| **AIOStreams' Jellyfin server** | The same, named "(synced)". |
| **AIOMetadata**, stock | It shows plain language names ("English (external)") instead of "(Subsync)" or "(synced)". It keeps each title's subtitle list and text for an hour, also its "no subtitles" answer: restart it once after setup (`docker restart aiometadata`), and set `JELLYFIN_SUBTITLE_TTL=300` in its environment to see synced copies within 5 minutes. A copy made when you first open a file is usually there at once if you pick the first subtitle of your language. |

## Update

```bash
./setup
```

The page after setup shows the version this machine runs and, when there is one, the version that is ready, with an
Update button. From the terminal:

```bash
./setup --update
```

Updating builds subsync again from the new version and restarts it; your answers, keys and stored copies stay. On a
public instance: updating builds AIOSubtitles again from the new version and restarts it; your answers, keys, sign-ups
and stored copies stay, and a copy of its databases is saved first. Nothing looks for a new version by itself: setup
asks git only when you open the page after setup or run `./setup --update`. The old version keeps running until the new
one is built and answers; if the build fails, the folder goes back to the old version.

This folder wasn't downloaded with git? Then it can't fetch a new version: download the new version, copy this folder's
state/ into it, and run ./setup there.

## Everyday commands

```bash
./setup
```

Where it is set up, the page after setup: its status, updates, your answers (Change your answers opens the steps again,
filled in) and removing it.

```bash
./setup --status
```

Shows whether subsync is running and what it has done today.

```bash
./setup --update
```

Updates to the newest version, if there is one.

```bash
./setup --uninstall
```

Takes subsync out of your config, deletes its own config and removes the container. It then lists what stays: the
subsync image, your web server's rule for subsync, and this folder with the copies of your config.

```bash
./setup --restore state/backups/FILE
```

Puts a saved copy of your config back (it asks for the config's password and saves the current one first).

```bash
docker logs aiosubtitles-subsync
```

subsync's log. It contains no links and no keys.

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
subtitle track use the audio route, if it is on: subsync detects speech in short audio parts of the file, fits the
subtitle to it, and checks the result on other parts it did not use for the fit. A copy is kept when, on those parts,
its typical error is 150 ms or less, at least 55 % of its lines are within 200 ms of the speech, few lines are
unmatched, and it moved the lines by at least a second.

## Settings

<!-- settings: written by python3 -I installer/docs.py from subsync/subsync/explain.py -->

Every setting affects only your own install: nothing is shared between AIOSubtitles installs, so no setting here helps or costs anyone else. The last group, public mode, is for running one service for many people; there how each copy was synced (its timing, never its text) is shared between everyone who plays the same file.

### The questions ./setup asks

Each question shows its explanation as it is asked. In an answers file (`./setup --answers FILE`) each one is a line `KEY=value`; see `docs/answers.example`.

- **`ROUTE`**: Install it for yourself (yourself, the default). This adds it to the AIOStreams you already run. Host a public instance (host). Runs here with its own sign-up page, so other people can use it with their own AIOStreams.
- **`AIOS_CONTAINER`**: Asked only when more than one AIOStreams runs in Docker on this machine: the one your apps use. subsync joins that container's Docker network and finds files through it, so the wrong one means it finds none of the files you play. Pick the one whose address your apps use.
- **`AIOS_URL`**: Asked only when setup can't find AIOStreams running in Docker on this machine: the address of AIOStreams' configure page, without /stremio/..., like http://127.0.0.1:3000 or https://aio.example.com. subsync talks to AIOStreams at this address, so it must work from this machine.
- **`AIOS_CONFIG_ID`**: The ID of the AIOStreams config your apps use: a long code like 1a2b3c4d-…, shown on AIOStreams' configure page when you save or load the config, and part of its install link (…/stremio/<ID>/…). You can paste the whole install link: setup takes the ID out of it and checks that the config exists. subsync is added to this config, and subsync's own config is made from it.
- **`AIOS_CONFIG_PASSWORD`**: The password you chose when you saved that config. Setup uses it to read the config and save the change, and doesn't store it; subsync gets its own config with its own random password. Removing subsync and restoring a saved copy ask for it again. If you put it in an answers file, keep that file private.
- **`LANGUAGES`**: The subtitle languages to sync, up to four, the first one first; English if you choose none. In an answers file, names or codes separated by commas, like English, Spanish, ru. For each language and each file you play, subsync downloads that language's subtitles from your subtitle sources and the ones it searches itself, and checks them against the file. Each extra language adds those downloads and a few seconds of work per file; the video file is still read only once.
- **`AUDIO`**: Also sync files that have no subtitle track of their own, by listening to their speech. Recommended: no, unless you often watch such files; most files have a track. Off: those files keep their original subtitles, and subsync reads about 1 MB of each file. On: after you pick such a file's "<Language> (Subsync)" entry, subsync reads short audio parts of it, up to the limit you choose next (1 GB recommended; counted in the daily limit and in your debrid service's traffic), and the copy is there the next time you open the file; files with too high a bitrate for that limit keep their originals.
- **`AUDIO_BUDGET`**: Asked only with the audio route on: the most it reads of one file, through your debrid service: 0.5, 1, 2 or 4 GB. Recommended: 1: it covers nearly every file with no subtitle track (web releases, MP4s, re-encodes); remuxes usually carry image subtitles and need no audio. Higher: very high-quality files such as 4K remuxes get synced too, but each can cost that much data and take longer than the 14 seconds an app waits, so the copy comes the next time you open the file. Lower: less data on a metered connection; more high-bitrate files keep their originals. Smaller files are read only as far as needed. A change makes subsync judge again the files it kept as originals.
- **`SUBDL_KEY`**: Optional: your free SubDL API key, from https://subdl.com/panel/api. With it, subsync also searches SubDL, which often has subtitles the other sources don't, so a copy that fits is more likely. Without it, SubDL isn't asked. Cost: one SubDL search for each file you play, plus a download per subtitle tried (at most three per file and language), counted against your key's own limit; the search sends the title's IMDb id. Kept only on this machine, in state/, readable only by your user.
- **`WYZIE_KEY`**: Optional: your free Wyzie Subs API key, from https://wyzie.io. With it, subsync also searches Wyzie Subs, which often has subtitles the other sources don't, so a copy that fits is more likely. Without it, Wyzie Subs isn't asked. Cost: one search per language for each file you play, plus a download per subtitle tried (at most two per file and language), counted against your key's own limit; the search sends the title's IMDb id. Kept only on this machine, in state/, readable only by your user.
- **`ADD_OPENSUBTITLES`**: Asked only when your config has no subtitle source, so AIOStreams gives subsync nothing to sync. Yes adds Stremio's official OpenSubtitles addon to your config: free, no account, it lists every language, and subsync syncs only the ones you picked. Recommended: yes. No: subsync works only from the sources it searches itself, and your apps get no original subtitles through AIOStreams. In Stremio, install your addon link once more afterwards so it starts asking for subtitles.
- **`PUBLIC_ADDRESS`**: The address your apps download synced subtitle files from. The suggestion sits behind the same web address as AIOStreams, like https://aio.example.com/subsync, which needs one rule in your web server (setup shows it); on a home network, this machine's own port works, like http://192.168.1.5:8790. It must work from every device you watch on, outside your home too if you watch there; otherwise entries show but fail to load. Only synced files and a health check are open at that address.
- **`CACHE`**: AIOStreams keeps every subtitle answer for 5 minutes. Yes turns that off for subsync only, so a copy made while you watch shows the next time you open the file instead of up to 5 minutes later; other subtitle sources keep their cache. It needs AIOStreams' admin sign-in and changes that one setting on your AIOStreams server. Recommended: yes if you have the admin sign-in. Cost: AIOStreams asks subsync every time, a few milliseconds.
- **`AIOS_USERNAME`**: Your AIOStreams sign-in (the account of its web page), asked only when this AIOStreams needs one to save configs, or for the subtitle cache setting (then an admin account). Used during setup and not stored.
- **`AIOS_PASSWORD`**: The password of that AIOStreams account. Used during setup and not stored. If you put it in an answers file, keep that file private.
- **`CONFIRM`**: Saves the changes shown here: subsync added to your config (after a backup copy) and subsync's own config created or updated. Until then nothing is changed: go back to change an answer, or leave and run ./setup again any time. Removing subsync and restoring a saved copy also ask before they change anything.
- **`TEST_READ`**: A small test read at the end: subsync reads the start and the index of one ready file (usually under 1 MB), as it does for every file you play. Yes: you see now that reading works and whether that file has its own subtitle track; your debrid service opens the file, so it may show in your debrid account's list. No (default): the first file you play is the first read.
- **`HOST_ADDRESS`**: The web address of your public instance, with https, like https://subtitles.example.com: people open it to sign up, and their apps download synced files from it. Your web server sends it to this machine; setup shows the one rule it needs. It goes into every personal link, so choose one you will keep: changing it later breaks the links people already added to their AIOStreams.
- **`REMOVE_OPENSUBTITLES`**: Asked when you remove subsync, if setup had added OpenSubtitles to your config. Yes takes it out again, leaving your config as it was before subsync; no keeps it, so your apps keep getting OpenSubtitles' subtitles.

### subsync's settings

./setup writes the first group into `state/subsync.env` from your answers, each line with its explanation above it; the others keep the defaults shown. To change one, add `NAME=value` to `state/subsync.env` and run `docker compose -f state/compose.yaml up -d`. Running ./setup again rewrites only the first group's lines and keeps yours.

#### Written by ./setup

- **`SUBSYNC_SOURCE_MANIFEST`** (required): The link of subsync's own AIOStreams config, which subsync finds files and subtitles through. ./setup makes that config from yours, without Usenet, Real-Debrid, subsync itself or anything that works in the background, and writes this. Required: subsync doesn't start without it. Keep it private: anyone with it can list through that config.
- **`SUBSYNC_SOURCE_PASSWORD`** (required): The password of subsync's own config: random, made by ./setup, never yours. Before every request through that config, subsync reads it with this password and checks that it holds no way to Real-Debrid; when the check fails or the password is wrong, nothing is listed and the log says why. Required: subsync doesn't start without it. Keep it private.
- **`SUBSYNC_PUBLIC_BASE`** (required): The address your apps download synced files from: your answer to ./setup's address question, like https://aio.example.com/subsync. subsync puts it in every entry it lists, so a wrong one makes entries show but fail to load. Required: subsync doesn't start without it.
- **`SUBSYNC_BASE_PATH`** (default: empty): The path part of that address, like /subsync, when your web server passes it on unchanged; subsync removes it from incoming requests. ./setup sets it from the address, and leaves it empty when subsync has its own port.
- **`SUBSYNC_ADDON_KEY`** (default: empty): A private key in the address of the routes that make subsync work: its manifest, its subtitle lists and its status. Without the key those routes answer "not found", so nobody who finds subsync's address can make it read files; synced files and /health stay open for your apps. Made by ./setup; keep it private. Empty: no key and every route open, safe only where nobody else can reach subsync.
- **`SUBSYNC_LANGUAGES`** (default `eng`): The languages to sync, as codes separated by commas, like eng,rus: ./setup writes your answer to its languages question. Each language is a separate sync for every file you play, with its own subtitle downloads and a few seconds of work; the video file is read only once.
- **`SUBSYNC_AUDIO_ROUTE`** (default `off`): Your answer to ./setup's audio question, on or off: whether files with no subtitle track of their own are synced from their speech, reading up to SUBSYNC_AUDIO_BUDGET_GB of audio each after you pick their "(Subsync)" entry. Recommended: off unless you often watch such files. Off: they keep their original subtitles and subsync reads about 1 MB per file.
- **`SUBSYNC_AUDIO_BUDGET_GB`** (default `1`): With the audio route on, the most audio subsync reads of one file, in GB: your answer to ./setup's audio limit question (0.5, 1, 2 or 4), written only while the audio route is on. Within it, high-bitrate files get shorter audio parts, or none when they would be too short to judge, and then keep their originals; a smaller file is read only as far as needed. Recommended: 1. Higher: very high-quality files such as 4K remuxes get copies too, at up to that much data each. Lower: less data, more such files keep their originals. Another number set here by hand works too; 0 removes the limit, which is not recommended.
- **`SUBSYNC_URL_REWRITE`** (default: empty): Rewrites the start of stream links, like https://aio.example.com=http://aiostreams:3000, so subsync reads files from AIOStreams over Docker on this machine instead of out through your web server and back. ./setup sets it when both run here and leaves it empty when they don't. A wrong rule makes every read fail, and files keep their original subtitles.
- **`SUBSYNC_SUBDL_KEY`** (default: empty): Optional: your free SubDL API key, from https://subdl.com/panel/api. With it, subsync also searches SubDL, which often has subtitles the other sources don't, so a copy that fits is more likely. Without it, SubDL isn't asked. Cost: one SubDL search for each file you play, plus a download per subtitle tried (at most three per file and language), counted against your key's own limit; the search sends the title's IMDb id. Kept only on this machine, in state/, readable only by your user.
- **`SUBSYNC_WYZIE_KEY`** (default: empty): Optional: your free Wyzie Subs API key, from https://wyzie.io. With it, subsync also searches Wyzie Subs, which often has subtitles the other sources don't, so a copy that fits is more likely. Without it, Wyzie Subs isn't asked. Cost: one search per language for each file you play, plus a download per subtitle tried (at most two per file and language), counted against your key's own limit; the search sends the title's IMDb id. Kept only on this machine, in state/, readable only by your user.
- **`SUBSYNC_PORT`** (default `8790`): The port subsync listens on: inside its container, or on the machine itself when AIOStreams uses host networking. Change it only when 8790 is taken, by giving ./setup's address question another port; ./setup then sets it.

#### Limits and safety

- **`SUBSYNC_DAILY_BUDGET_GB`** (default `50`): The most subsync reads from files in one day (UTC), in GB received. When it's spent, reading stops until the next day and new files keep their original subtitles. 50 is far above normal use (about 1 MB per file, a few MB when the track's lines are pictures; up to 1 GB per file with the audio route), so it only catches something going wrong. Lower it if your debrid service or server has a tight traffic limit.
- **`SUBSYNC_INDEX_CAP_MB`** (default `16`): The most subsync reads of one file to find its subtitle tracks (the start, the index, and 64 KB at each end), in MB. Most files need about 1 MB; files with very many tracks need more, and one needing more than this keeps its original subtitles. Recommended: 16. Higher: a few more such files get copies, each costing that much more data.
- **`SUBSYNC_FILE_CAP_GB`** (default `6`): The most subsync reads of one file in all when the audio route works on it, in GB: an outer safety limit, since the audio limit (SUBSYNC_AUDIO_BUDGET_GB, at most 4 GB from ./setup) normally stops first. Recommended: 6. 0 removes it, which is not recommended.
- **`SUBSYNC_403_BACKOFF_S`** (default `900`): After a debrid service refuses a read (HTTP 403 or 429, usually a per-account limit), subsync stops all reads for this many seconds, doubling with each refusal in a row up to 6 hours; the refused file itself waits 1, 6, then 24 hours. Recommended: 900 (15 minutes). Shorter: copies resume sooner, but subsync keeps knocking on a limit your own playback needs too.
- **`SUBSYNC_RETRY_STOPPED_S`** (default `600`): A file whose work stopped because the daily limit was spent, or reads were paused after a refusal, is tried again after this many seconds. Recommended: 600 (10 minutes). Shorter only means more checks of a pause that is still on.
- **`SUBSYNC_READ_TIMEOUT_S`** (default `60`): How long one read of a file may wait for an answer, in seconds, before it counts as failed; a failed file is tried again 1, 6 and 24 hours later. Recommended: 60. Shorter: slow services fail more often.
- **`SUBSYNC_SIZE_SLACK_MB`** (default `5.5`): How far a file's listed size may be from its real size and still be taken for the same file, in MB; listings round sizes to 0.01 GiB, up to 5.4 MB off. Recommended: 5.5. Lower: rounded listings stop matching and those files get no copy. Higher: another file of the same name could be taken, though its own hash still catches that.
- **`SUBSYNC_STORE_CAP_MB`** (default `2048`): Disk space for synced copies, in MB; past it, the copies served longest ago are deleted and made again when needed, which costs a new read. 2048 holds about 20,000 copies. Lower it on a small disk.
- **`SUBSYNC_SKIP_ADDONS`** (default: empty): Names of AIOStreams addons, separated by commas, whose listed files subsync never reads; empty by default. Use it for an addon whose files you don't want read; those files get no synced copy.

#### Which subtitles are tried

- **`SUBSYNC_EXTRA_SOURCES`** (default `osv3,osv3plus,tsdb,gestdown`): Subtitle sites AIOSubtitles searches itself, beside the subtitle addons in your AIOStreams config: osv3 (OpenSubtitles through Stremio's own addon), osv3plus (OpenSubtitles V3+), tsdb (TheSubtitleDB) and gestdown (Gestdown, series only); none needs a key. Recommended: all four: they often have a subtitle made from your exact release, which fits best. Remove one to stop asking it. Each search sends the title's IMDb id, season and episode and your languages; the OpenSubtitles ones also get the file's fingerprint (hash), size and name, as your player sends them, and TheSubtitleDB the torrent's hash and the file's name. A site that is already an addon in your config isn't asked twice.
- **`SUBSYNC_SOURCE_TIMEOUT_S`** (default `5`): How long AIOSubtitles waits for each subtitle site it searches itself, in seconds. A site that answers later is left out for this play and its answer is kept for the next one, so a slow site never makes your first subtitle late. Recommended: 5. Longer: slow sites count more often, but your first subtitle can come later. A site that fails three times in ten minutes is paused for ten minutes, then tried again.
- **`SUBSYNC_SKIP_CONFIG_SOURCES`** (default `on`): On: a subtitle site that is already an addon in your AIOStreams config (for example OpenSubtitles or SubDL) is searched only once, through your config and with your settings for it. Recommended: on: half the requests to that site and no repeated subtitles. Off: AIOSubtitles also asks it itself, with its own settings, which can find subtitles your settings for it leave out, at the cost of a second search.
- **`SUBSYNC_SUBSOURCE_KEY`** (default: empty): Optional: a free SubSource API key (create one under API in your account on subsource.net). With it, AIOSubtitles also searches SubSource, which has many subtitles the other sites don't, so a copy that fits is more likely. Without it, SubSource isn't asked. Cost: two requests per title and one per subtitle tried, counted against the key's limit (60 a minute, 7,200 a day for a free key); at most four subtitles per file and language come from it. The search sends the title's IMDb id and season. The key is sent in a header, never shown or logged.
- **`SUBSYNC_TSDB_KEY`** (default: empty): Optional: a free TheSubtitleDB key (sign up on thesubtitledb.org). Searching TheSubtitleDB needs no key; the key raises its downloads from 1,000 an hour to 100,000 a day. Recommended: leave it empty for your own install, which never comes near that limit; set it for a service many people use. The key is sent in a header, never in an address, and is never shown or logged.
- **`SUBSYNC_SKIP_AI`** (default `on`): On: subtitles marked as AI-made, machine-translated or forced (covering only foreign-language parts) are never tried. Recommended: on: they read badly or cover only part of the film. Off: they can become synced copies like any other.
- **`SUBSYNC_RANK_BY_RELEASE`** (default `on`): On: subtitles made for this exact release (by its file hash or release name) are tried first. Recommended: on: they usually fit as they are, so your copy is ready sooner. Off: subtitles are tried in your sources' order.
- **`SUBSYNC_BEST_FIT`** (default `on`): On: subsync judges every subtitle it tries and lists the best-fitting copy first. Off: it lists the first copy that passes, in your sources' order, and stops there. Recommended: on: the first entry you see fits best. Cost: every try is made, a few more downloads and seconds per file.
- **`SUBSYNC_MAIN_ALTERNATIVES`** (default `3`): With best fit on, how many synced copies of your first language are listed, best first. Recommended: 3, so you can switch when the first has a problem the check can't see, like a poor translation. 1: a shorter list.
- **`SUBSYNC_GATE_TRIES`** (default `20`): With best fit on, how many subtitles per language subsync checks as they are (a fraction of a second each, no re-timing), looking for one that already fits. Recommended: 20. Higher: more downloads per file for a slightly better chance of an exact fit.
- **`SUBSYNC_MAX_TRIES`** (default `6`): How many subtitles per language and file subsync re-times with alass (the timing tool) before it stops; each try is a download and a few seconds of work. Recommended: 6. Higher: a better chance on hard files, at more time and downloads. Lower: faster, and more files keep their originals.

#### How copies are made and checked

- **`SUBSYNC_FAST_PATH`** (default `on`): On: each subtitle is first checked as it is, and one that already fits is listed at once without re-timing. Recommended: on: faster, and a fitting original is never altered. Off: every subtitle is re-timed first.
- **`SUBSYNC_SHIFT_PATH`** (default `on`): On: before full re-timing, subsync tries moving the whole subtitle by one steady amount, the common case of a film a second early or late. Recommended: on: fast and exact when it applies. Off: full re-timing every time, slower.
- **`SUBSYNC_TEXT_PAIRING`** (default `on`): On: when your subtitle and the file's own subtitle track share a language and timing alone can't settle it, subsync reads the words of up to 120 of the track's lines, once per file and language (up to 240 KB of text; on an image track, with a reader installed for the language, each line's picture, up to 64 KB), and pairs your subtitle's lines with those that say the same thing, even where lines are split or joined differently. A copy whose words disagree with its timing isn't offered. Words are kept only while the file is worked on. Recommended: on. Off: lines are paired by timing alone, nothing extra is read, and a copy only words could refuse may be offered.
- **`SUBSYNC_SECTIONS`** (default `on`): On: a subtitle made for a slightly different cut of the film (a few seconds added or cut here and there, other ads or recaps) is fitted in up to 8 sections, each moved by its own amount and each checked on its own; a copy is offered only when every section passes and the whole copy does too, and a subtitle that lacks a scene of your file is still never offered. Recommended: on. Off: the whole film is moved by one amount, then by the general re-timing. Nothing extra is read either way.
- **`SUBSYNC_SNAP`** (default `on`): On: after re-timing, each line moves to start exactly where the file's own matching line starts, wherever that match is clear. Recommended: on: lines appear right on cue. Off: lines keep their author's timing inside the re-timed blocks.
- **`SUBSYNC_SNAP_WITHIN`** (default `0.95`): The share of lines that must start within 200 ms of the file's track for a copy whose lines were snapped (0.95 = 95 %). Snapped lines look better to the check than they are, so they need more than the 90 % other copies need. Recommended: 0.95. Lower: more copies offered, a few slightly off.
- **`SUBSYNC_BETTER`** (default `on`): On: a copy that misses the 90 % bar is still offered when it is clearly better than the original: its typical error is 200 ms or less and at most half the original's, at least 85 % of its lines pair with the file's lines, and no part of the film is missing. Recommended: on: such a copy is a plain improvement. Off: only copies that meet the bar; the rest keep their originals.
- **`SUBSYNC_BETTER_PAIRED`** (default `0.85`): For "clearly better": the share of the copy's lines that must pair with a line of the file's track (0.85 = 85 %). Recommended: 0.85. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_BETTER_MEDIAN_MS`** (default `200`): For "clearly better": the copy's typical error, the median distance of its line starts from the file's, must be at most this many ms. Recommended: 200. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_BETTER_RATIO`** (default `0.5`): For "clearly better": the copy's typical error must be at most this share of the original's (0.5 = half). Recommended: 0.5: a smaller gain isn't worth replacing the original. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_BETTER_CROSS_MEDIAN_MS`** (default `300`): For "clearly better" when the only track to check against is in another language (a translation checked against the English track): the typical error must be at most this many ms. Recommended: 300, looser than 200 because translations split and merge lines. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_BETTER_CROSS_PAIRED`** (default `0.75`): For "clearly better" against another language's track: the share of the copy's lines that must pair (0.75 = 75 %). Recommended: 0.75. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_FALLBACK_REFERENCE`** (default `eng`): When a file has no track in a subtitle's language, the languages whose track it is checked against first, as codes separated by commas. Recommended: eng: English tracks are the most common and the most complete. Empty: such subtitles are checked against no other language's track.
- **`SUBSYNC_PREFER_PLAIN_TRACK`** (default `on`): On: copies are checked against the file's plain subtitle track rather than its SDH track (the one with sound descriptions like [door slams], which has extra lines). Recommended: on: a cleaner comparison and fewer copies refused for lines the SDH track adds.
- **`SUBSYNC_IMAGE_REF`** (default `on`): On: files whose only subtitles are images (PGS or VobSub, common in Blu-ray rips) are checked against those images' timings. Recommended: on. Off: such files keep their originals, unless the audio route is on.
- **`SUBSYNC_GATE_SPLITS`** (default `on`): On: when a subtitle splits one of the file's lines in two, the second half counts as matched. Recommended: on: otherwise good copies that split lines fail the check and you get the original.
- **`SUBSYNC_LIST_IN_SYNC`** (default `on`): On: an original further down the list that already fits the file is listed as a synced copy too. Recommended: on: you can see that it fits. Off: it stays listed only under its original name.
- **`SUBSYNC_PARALLEL_LANGUAGES`** (default `on`): On: your languages are synced at the same time. Recommended: on: copies are ready sooner. Off: one after another, less work at once on a small server.
- **`SUBSYNC_ANCHORED_GUESS`** (default `on`): With several languages, while a file waits for the audio route: your other languages are shifted onto the first language's best guess, so all of them start in step. Recommended: on. Off: they stay as they are until their own copies are ready.

#### When the audio route works

- **`SUBSYNC_AUDIO_ON_PICK`** (default `on`): With the audio route on: on, subsync reads a file's audio only after you pick its "(Subsync)" entry, which shows you are playing it; off, on any subtitle request, including the ones AIOMetadata sends for a version you never play. Recommended: on: no audio is read for files you only browse. Off: copies come sooner, at up to 1 GB read for every file merely opened.
- **`SUBSYNC_PICK_WAIT_MS`** (default `14000`): How long your app's download of a "(Subsync)" entry waits for the synced copy, in ms, before it gets the original instead. Recommended: 14000 (AIOMetadata gives up after 15 s). Lower: the original comes sooner and more often.
- **`SUBSYNC_PENDING_ENTRY`** (default `off`): On: instead of "(Subsync)" entries, a "(synced)" entry is listed for a file still being worked on, served as the original until the copy is ready, and subtitle lists wait up to SUBSYNC_PENDING_WAIT_MS. Recommended: off: the "(Subsync)" entry already gives the copy once it is ready, and lists don't wait.
- **`SUBSYNC_PENDING_WAIT_MS`** (default `1000`): With pending entries on, how long a subtitle list waits for a file's work before answering, and how long a pending entry waits for its copy, in ms. Recommended: 1000. Longer: more copies on the first try, slower lists.
- **`SUBSYNC_AUDIO_LEAD_MS`** (default `190`): Audio route: in good releases a subtitle appears about 190 ms before the speech starts, so copies timed from speech are moved this much earlier. Recommended: 190. 0: lines appear exactly as speech starts, a little late to the eye.
- **`SUBSYNC_AUDIO_EXTRA_WINDOWS`** (default `12`): Audio route: when the first check against the speech is a close call (just short of the bar, or passing on few lines), up to this many more 30-second pieces of the sound are read to decide, within your audio limit (each piece is about half a percent of a two-hour film's file: about 10 MB of a 2 GB film), and the file is judged again on all of them. Recommended: 12. 0: the first check decides and nothing more is read. More: surer verdicts on quiet or long films, more data read on those films only, and their copies take a few seconds longer.
- **`SUBSYNC_AUDIO_BETTER`** (default `on`): Audio route: on, a copy that misses the standard bar against the speech is still offered when it is clearly closer to the speech than the original (the five rules below). Recommended: on. Off: only copies that meet the standard bar; more of these files keep their originals.
- **`SUBSYNC_AUDIO_BETTER_MATCHED`** (default `25`): Audio route, "clearly closer": at least this many of the copy's lines must match speech. Recommended: 25. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_AUDIO_BETTER_MISS`** (default `0.30`): Audio route, "clearly closer": at most this share of the copy's lines may match no speech (0.30 = 30 %). Recommended: 0.30. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_AUDIO_BETTER_MEDIAN_MS`** (default `150`): Audio route, "clearly closer": the copy's typical error from the speech must be at most this many ms. Recommended: 150. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_AUDIO_BETTER_WITHIN`** (default `0.55`): Audio route, "clearly closer": at least this share of lines must start within 200 ms of the speech (0.55 = 55 %), lower than for a subtitle track because speech detection is less exact. Recommended: 0.55. Higher or lower than recommended trades how many copies are offered against how closely they fit.
- **`SUBSYNC_AUDIO_BETTER_MIN_SHIFT_S`** (default `1.0`): Audio route, "clearly closer": the copy must have moved the lines by at least this many seconds; a smaller change isn't worth replacing the original. Recommended: 1.0.

#### Programs and addresses

- **`SUBSYNC_DATA`** (default `/data`): Where subsync keeps its database and synced copies inside its container, on a Docker volume that survives updates. Leave it.
- **`SUBSYNC_ALASS`** (default `alass`): The alass program subsync re-times subtitles with, built into its image. Leave it.
- **`SUBSYNC_FFMPEG`** (default `ffmpeg`): The ffmpeg program that decodes short audio parts for the audio route, built into its image. Leave it.
- **`SUBSYNC_VAD_MODEL`** (default: empty): The file of the speech-detection model (Silero VAD) the audio route uses; empty takes the one in the image. Leave it.
- **`SUBSYNC_TSDB_URL`** (default `https://api.thesubtitledb.org`): The address of TheSubtitleDB's API, a source subsync searches itself. Change it only if the service moves.
- **`SUBSYNC_GESTDOWN_URL`** (default `https://api.gestdown.info`): The address of Gestdown's API (series subtitles), a source subsync searches itself. Change it only if the service moves.
- **`SUBSYNC_CINEMETA_URL`** (default `https://v3-cinemeta.strem.io`): The address of Cinemeta, Stremio's catalogue, which subsync asks for a series' TheTVDB number before it searches Gestdown. Change it only if it moves.
- **`SUBSYNC_OSV3PLUS_URL`** (default `https://opensubtitles.stremio.homes`): The address of OpenSubtitles V3+, a source subsync searches itself. Change it only if the service moves.
- **`SUBSYNC_OSV3_URL`** (default `https://opensubtitles-v3.strem.io`): The address of Stremio's own OpenSubtitles addon, a source AIOSubtitles searches itself. Change it only if it moves.
- **`SUBSYNC_SUBSOURCE_URL`** (default `https://api.subsource.net/api/v1`): The address of SubSource's API, used only with a SubSource key. Change it only if the service moves.
- **`SUBSYNC_SUBDL_URL`** (default `https://api.subdl.com`): The address of SubDL's search API, used only with a SubDL key. Change it only if the service moves.
- **`SUBSYNC_SUBDL_DOWNLOAD_URL`** (default `https://dl.subdl.com`): The address SubDL's subtitle files are downloaded from, used only with a SubDL key. Change it only if the service moves.
- **`SUBSYNC_WYZIE_URL`** (default `https://sub.wyzie.io`): The address of Wyzie Subs' API, used only with a Wyzie key. Change it only if the service moves.

#### Public mode (one service for many people)

- **`SUBSYNC_PUBLIC`** (default `off`): Public mode: one AIOSubtitles service for many people, like aiosubtitles.com. Each person signs up on its web page with their AIOStreams link and password and gets a personal addon link; subsync then works through a separate sync config it makes on their AIOStreams. Leave it off for your own install; setup turns it on when you host a public instance. On, the service needs SUBSYNC_SECRET_KEY and SUBSYNC_PUBLIC_BASE, keeps the audio route off, and shares how each copy was synced (its timing, never its text) between everyone who plays the same file, so the more people use it, the more files are ready when someone presses play. It holds other people's links, so it needs a server you keep up to date and backed up.
- **`SUBSYNC_SECRET_KEY`** (default: empty): The service's secret, at least 32 random bytes written as base64url; setup makes one and keeps it in state/subsync.env, readable only by your user (by hand: what python3 -c "import secrets; print(secrets.token_urlsafe(32))" prints). It encrypts each person's record together with their personal link, so neither the database nor this secret alone opens a record. Required in public mode; never logged or shown. Keep that file in your backups: if the secret is lost or changed, everyone has to sign up again.
- **`SUBSYNC_PUBLIC_INVITE`** (default: empty): When set, the sign-up page asks for this code, so only people you gave it to can sign up, for example while you and your testers try the service. Empty lets anyone sign up. It doesn't affect people who already signed up. Recommended while testing; remove it to open the service.
- **`SUBSYNC_MAX_USERS`** (default `200`): How many people can be signed up at once; past it the sign-up page says the service is full. Each person costs a little disk and, while they play, a worker, so the limit keeps the server within what it can carry. Raise it only after watching memory, processor use and the daily read total for a while.
- **`SUBSYNC_SIGNUPS_PER_HOUR`** (default `5`): Sign-up attempts allowed per internet address per hour, counted before anything is fetched. Each attempt makes the service contact that person's AIOStreams several times, so this stops one address from flooding the service or someone else's AIOStreams. 5 is plenty for someone who mistypes a password; lower is stricter, higher helps many people behind one shared address.
- **`SUBSYNC_USER_FILES_PER_DAY`** (default `60`): New files read per person per day (UTC). Files already in the shared store don't count, and neither does serving a copy. Past it, that person gets only copies the store already has until the next day; nobody else is affected. 60 is a long day of episodes; it keeps one person, or a leaked link, from using up the service's daily reads.
- **`SUBSYNC_USER_DAILY_MB`** (default `1000`): Megabytes read per person per day (UTC) through their own sync config. Reading a file's subtitle track takes about 1 MB, a few MB when the track's lines are pictures; 1,000 is 60 files at the most one file's index may take (16 MB), so it never stops someone before the file limit, and it stops someone whose files have unusual layouts from using most of the service's daily total. Past it, that person gets only copies already in the store until the next day.
- **`SUBSYNC_MAX_LANGUAGES`** (default `4`): How many languages each person may choose, from 1 to 4. Each language is a separate sync for every file they play: its own subtitle downloads and a few seconds of work, while the file itself is read once. Recommended: 4, the most the pages offer; lower it only when the server can't keep up. A lower limit applies when people next save their languages; until then they keep the ones they have.
- **`SUBSYNC_USER_QUEUE`** (default `3`): Files one person may have waiting at once. A request past it is answered with what the shared store already has and starts nothing. Everyone's files wait for the same few places (SUBSYNC_SYNCS_AT_ONCE), so a longer queue for one person delays everyone else; 3 covers someone opening a few episodes in a row.
- **`SUBSYNC_ACTIVE_WORKERS`** (default `100`): How many people can have a worker (the part that finds and syncs their files) running at once. A worker with nothing to do stops after a while (30 minutes by default). Past the limit, a person's requests get only copies already in the shared store until a worker frees up. Each worker takes about 2.5 MB of memory, so 100 take about 250 MB; keep the limit within what the server's memory allows.
- **`SUBSYNC_WORKER_IDLE_MIN`** (default `30`): Minutes without a request before a person's worker stops and their opened record leaves memory. Shorter frees memory sooner and keeps opened records in memory for less time; longer saves reopening the record when someone pauses between episodes. Reopening is quick, so 30 is a fair middle.
- **`SUBSYNC_USER_KEEP_DAYS`** (default `180`): Days without a single request before a person's record is deleted. Their sync config can't be removed from their AIOStreams then, because opening the record needs their personal link; the privacy page says so. Shorter keeps less data about people who left; longer spares people who come back after a long break from signing up again.
- **`SUBSYNC_TRUSTED_PROXY`** (default `127.0.0.1`): The addresses of the web server in front of subsync, comma-separated. Only requests from these may say, in their X-Forwarded-For header, which address they came from, and the sign-up limit per address counts that one. Set it to your web server's address as subsync sees it, or to gateway when the web server reaches subsync through a port Docker publishes: subsync then trusts its Docker network's gateway, found at start, with no fixed network to set up. A wrong value makes every visitor count as one address, or lets visitors choose their own.
- **`SUBSYNC_MAX_CONNECTIONS`** (default `256`): Requests handled at once. Each one takes a thread and some memory, so past it the service answers "busy, try again" (HTTP 503) at once instead of running out of memory. Apps ask for subtitles again on their own. 256 leaves room for 200 people's apps asking at once; raise it together with the memory the service may use.
- **`SUBSYNC_RECHECK_HOURS`** (default `24`): How often, at most, each person's AIOStreams is checked again while they use the service, in hours; 0 turns the check off. A check asks their AIOStreams two things (does their config still exist, does AIOSubtitles' own config there still open), repairs a changed link, and tells the person on their settings page and in their subtitle lists what only they can fix; for people who let AIOSubtitles keep their password it also rebuilds AIOSubtitles' config from theirs. Shorter notices changes sooner, at more calls to every AIOStreams; people who don't use the service aren't checked.
- **`SUBSYNC_SYNCS_AT_ONCE`** (default `auto`): How many files the service works on at once for everyone, or auto for one per processor it may use. Recommended: auto, since one sync keeps about one processor busy. More lets people who play new files at the same time wait less, at more processor use and more requests to each AIOStreams at once; one public AIOStreams shared by many people limits how many requests one address may make, so give a number when the processors are many and that AIOStreams is shared. A torrent's file is never read twice at once: whoever plays it second waits for the first one's work and reads nothing.

### Fixed rules

These are not settings: AIOSubtitles always works this way.

- **Real-Debrid is never used.** AIOSubtitles never makes a single call to Real-Debrid: no search, no cache check (asking whether a file is ready), no download, no file read, because it reads parts of each file you watch from the machine it runs on, and Real-Debrid's terms forbid using an account from a server, VPS or cloud service unless you pay extra for "Remote Traffic"; Real-Debrid records every connection and suspends accounts that break its terms. With Real-Debrid beside another service, such as TorBox, subsync uses only the other one, so a file you play from Real-Debrid gets a synced copy only when that service has the same file ready. With Real-Debrid as your only debrid service, ./setup stops. subsync also checks its config before every request through it, skips files listed as Real-Debrid's, and refuses and logs any request to real-debrid.com. Your apps play Real-Debrid files as before.
- **Addons added by link must show their debrid service.** An addon you added to AIOStreams by its link (a custom addon, or another AIOStreams) carries its own settings in that link, sometimes with a debrid key. subsync's config keeps such an addon only when its link names a debrid service other than Real-Debrid and doesn't name Real-Debrid; one whose link names Real-Debrid, or is encrypted so nobody can tell, is left out, because AIOStreams would let it search Real-Debrid for subsync. ./setup names each addon it leaves out. Your own config and apps keep every addon; subsync only finds fewer files when a left-out addon was the only one listing a file. Addons built into AIOStreams are not affected: they take their debrid service from your service list.
- **Usenet is never read.** subsync never reads through Usenet: its config leaves out Usenet sources (Newznab indexers, NZBHydra, Easynews and similar) and Usenet services (NzbDAV, AltMount and similar). Why: every listing through an indexer spends calls of its daily limit, and opening a Usenet file usually starts a download of the whole file. A file you play from Usenet gets a synced copy only when your debrid service has the same file ready. Your indexer calls and download queue stay untouched; there is no switch for this.
- **Only ready debrid files are read.** subsync reads a file only when your debrid service already has it ready (cached), never one the service would first have to download, and never one found in your debrid account's own download list. So subsync never adds a download to your account. Files played from plain torrents or web links get no synced copy.
- **Only MKV and MP4 files.** subsync reads the subtitle tracks of MKV and MP4 files only, the formats nearly all streaming releases use. Other files (AVI, M2TS and the like) keep their original subtitles, after one small read that shows the format.
- **A copy is offered only after it passes the check.** Each synced copy is checked against the file itself before it is offered: at least 90 % of its lines must start within 200 ms (a fifth of a second) of the file's own subtitle track, spread over the whole film (95 % when its lines were snapped onto the track), or it must be clearly better than the original (SUBSYNC_BETTER). Where the track is in the subtitle's language and timing alone leaves doubt, the lines' words must agree too (SUBSYNC_TEXT_PAIRING); a copy fitted in sections for another cut must pass in every section (SUBSYNC_SECTIONS). Line ends are measured and recorded, but don't decide. Files without a subtitle track are checked against their speech instead (the audio route). A copy that fails is never offered: you get the original subtitles, as without subsync.
- **At most four languages.** ./setup syncs up to four languages. Each one is a separate sync for every file you play, with its own subtitle downloads and a few seconds of work, though the video file is read only once.
- **subsync's config starts nothing in the background.** subsync's own AIOStreams config has failover, preloading, pre-caching of the next episode, service wrap, cache-and-play and owned-file checks turned off, whatever your config uses. So reading a listed file opens that one file and nothing else, and subsync never starts a download. Your own config keeps its settings.
- **Nothing changes without your yes.** ./setup shows each change to your AIOStreams config before saving it, saves a copy of your config first (in state/backups/), and changes nothing if you answer no. ./setup --restore FILE puts a saved copy back; ./setup --uninstall takes subsync out again.
- **Links and keys stay private.** subsync's log never contains stream links, config addresses, keys or passwords, and the files in state/ that hold them can be read only by your user. The routes that make subsync work (its subtitle lists and status) answer only under a private key in their address, so nobody who finds subsync's address can make it read files; only the synced files and a health check are open.
- **Public mode: your AIOStreams password is used once.** When you sign up or refresh, AIOSubtitles uses your AIOStreams password in that request and then forgets it, unless you tick the box that lets it keep the password: then it is kept encrypted in your record, never in a log, until you untick it. It needs it to read your config, to make a separate config for AIOSubtitles on your AIOStreams (without Real-Debrid, Usenet, AIOSubtitles itself or anything that starts work in the background, so opening a file opens only that file) and, if you leave the box checked, to add AIOSubtitles to your own config. Without that separate config, a file that failed to open could make your AIOStreams move on to another source, Real-Debrid or a Usenet download among them.
- **Public mode: your AIOStreams is checked about once a day.** While you use AIOSubtitles, it checks your AIOStreams at most once a day, in the background: whether your config still exists (a question that needs no password) and whether its own config there still opens. It repairs a changed link by itself and tells you what only you can fix, on your settings page and with one "AIOSubtitles: action needed" entry in your subtitle lists. Without a kept password it can't see services or addons you add or remove, so press Refresh after such a change; with one, it rebuilds its own config from yours by itself, leaving Real-Debrid and Usenet out, and never changes your config.
- **Public mode: Real-Debrid is left out.** If your AIOStreams uses Real-Debrid, AIOSubtitles leaves it out of its own config and never reads a Real-Debrid file, because it reads parts of each file you watch from the machine it runs on, and Real-Debrid's terms forbid using an account from a server, VPS or cloud service unless you pay extra for "Remote Traffic"; Real-Debrid records every connection and suspends accounts that break its terms. It syncs only files from your other services: a file you play from Real-Debrid gets a synced copy when another of your services has the same file ready. With Real-Debrid as your only debrid service, AIOSubtitles reads nothing for you: you get only the copies already synced for someone else who played the same release. Your own config and apps keep Real-Debrid as before, and your AIOStreams still searches it when you browse.
- **Public mode: a few files at once.** The service works on a set number of files at once for everyone (SUBSYNC_SYNCS_AT_ONCE), in the order people asked; two people playing the same file of a torrent never read it twice at once: the second gets the first one's work. A file's subtitle track is about 1 MB (a few MB when the track's lines are pictures, whose words AIOSubtitles reads on its own server and doesn't keep) and is synced in seconds. Files already synced for someone are served at once. When many people play new files together, the synced copy can come after your app has picked a subtitle; the next time you open the file, it is there.
- **Public mode: limits per person.** Each person can have a few files waiting and read a set number of new files and megabytes a day (UTC), by default 3 waiting, 60 files and 1,000 MB, and the whole service reads at most its own daily total; files already synced for someone don't count. Past a limit, you get only copies already synced for someone until the next day, and nobody else is affected. Sign-up attempts are limited per internet address, and the service takes a set number of people. Your settings page shows today's use.
- **Public mode: what is kept, and for how long.** AIOSubtitles keeps one record per person, encrypted so that it opens only with that person's personal link: the address of their AIOStreams, the id of their config, AIOSubtitles' own config there and its password, and their languages, plus how much was read for them each day. Your AIOStreams password is never kept. For each synced subtitle only its new timing (never its text), with the names and sizes of the files it fits, is shared and kept 180 days after last use, without who played them; logs name the titles synced, never who asked. Your record is deleted when you delete it on your settings page, or 180 days after your last request.
- **Public mode: the timing is shared, not the subtitles.** When someone already synced a subtitle for the file you play, AIOSubtitles reuses that work: it keeps only how the subtitle was re-timed, never its text. Your copy is made from the original subtitle, downloaded again for you from its site, with that timing, so no new check is needed. This follows the subtitle sites' rules, which don't allow one person's stored copy to be handed to others, and costs one small download. If the site changed or removed the subtitle since, you don't get that copy.
- **The same subtitle counts once.** When the same subtitle comes from two places, for example from an addon in your config and from a site AIOSubtitles searches itself, it is checked and listed once. Same means the same words at the same times: another text encoding, line ending or a credit line such as a site's address at the start or end doesn't make it a different subtitle, while another timing does. The copy kept is the first one tried, which is the one that knows most about your release. This saves time and keeps your subtitle list free of repeats.
- **Which subtitles are tried first.** For each language, AIOSubtitles tries first the subtitle your config lists first, then the ones made for your exact file (matched by its fingerprint or torrent, or with as many lines as the file's own track), then those of your release, then the rest. Among equally likely ones, your config's addons come first, then OpenSubtitles, TheSubtitleDB, SubDL, SubSource, Gestdown and Wyzie. With no subtitle addon in your config, no subtitle has a place of its own as your first.
- **A failing subtitle site is paused.** A site AIOSubtitles searches itself that fails three times within ten minutes (no answer, or an error) is skipped for ten minutes, then tried with one request: if it answers, it is used again; if not, it waits another ten minutes. A site that asks AIOSubtitles to slow down waits as long as it asks, at most an hour. A site that simply has no subtitles for a title isn't failing. Your other sources keep working meanwhile, so a broken site can't make your subtitles late.
- **Sites with limits give each file only a few subtitles.** Some sites allow only so many requests or downloads: SubDL 300 downloads a day for each internet address and 2,000 searches a day for each key, SubSource 7,200 requests a day, Wyzie 1,000. AIOSubtitles counts them and takes at most three subtitles per file and language from SubDL, four from SubSource and two from Wyzie, the likeliest first. Near a site's limit, only subtitles made for your exact file or release are taken from it; at the limit it rests until its count starts again. On a service many people use, someone who already had their share of a site gets it only for those likeliest subtitles, so one person can't use up a site for everyone.

<!-- end of settings -->

## Folder

| Path | What it holds |
|---|---|
| `setup` | Starts the installer, `installer/wizard.py`. |
| `installer/` | The installer, written in plain Python with no packages to install. Its tests run with `python3 -m unittest discover -s installer/tests -t .`; `python3 -I installer/docs.py` rewrites the Settings section above and `docs/answers.example` from `subsync/subsync/explain.py`. |
| `subsync/` | The service. Its Docker build runs its own tests and stops if any test fails. |
| `state/` | Your settings, keys and config backups, readable only by you. Never share or commit this folder. |

## Licence

AGPL-3.0-or-later; see `LICENSE` and `NOTICE.md`.
