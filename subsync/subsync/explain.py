"""One explanation for every setting and every fixed rule, written once and shown wherever the user meets it: with each
./setup question (and again on "?"), in docs/answers.example, in the README's settings section and above each line of
state/subsync.env. Settings takes its defaults from here; tests fail when a setting has no explanation or a documented
default differs from the code's.

Each text says what the setting does, what to choose and why, what changes either way (what you see, what is read or
downloaded, what gets faster or slower) and what it costs. Nothing in AIOSubtitles is shared between installs, so every
setting changes only the subtitles of the install it is set on; in public mode (one service for many people) how each
copy was synced (its timing, never its text) is shared between everyone who plays the same file.

No packages besides the standard library, so ./setup imports this module too.
"""
from typing import NamedTuple

from .langs import DEFAULT_LANGUAGE
from .langs import name as lang_name
from .realdebrid import REASON
from .version import PROJECT


def plural(n, one, many=None):
    """The word for n of a thing, as a text says it after the number: `one` for 1, else `many` (by default `one` +
    "s"): "1 day", "30 days", "1 search", "10 searches"."""
    return one if n == 1 else many or one + "s"


# ------------------------------------------------------------------------------------------- fixed rules

RULES = [
    ("Real-Debrid is never used",
     "AIOSubtitles never makes a single call to Real-Debrid: no search, no cache check (asking whether a file is ready), "
     f"no download, no file read, because {REASON}. With Real-Debrid beside another service, such as TorBox, subsync uses only the other one, so a file you play from "
     "Real-Debrid gets a synced copy only when that service has the same file ready. With Real-Debrid as your only "
     "debrid service, ./setup goes on only when you turn Usenet on; subsync then reads only files you play from "
     "Usenet. subsync also checks its config before every request through it, skips files "
     "listed as Real-Debrid's, and refuses and logs any request to real-debrid.com. Your apps play Real-Debrid files "
     "as before."),
    ("Addons added by link must show their debrid service",
     "An addon you added to AIOStreams by its link (a custom addon, or another AIOStreams) carries its own settings in "
     "that link, sometimes with a debrid key. subsync's config keeps such an addon only when its link names a debrid "
     "service other than Real-Debrid and doesn't name Real-Debrid; one whose link names Real-Debrid, or is encrypted "
     "so nobody can tell, is left out, because AIOStreams would let it search Real-Debrid for subsync. ./setup names "
     "each addon it leaves out. Your own config and apps keep every addon; subsync only finds fewer files when a "
     "left-out addon was the only one listing a file. Addons built into AIOStreams are not affected: they take their "
     "debrid service from your service list."),
    ("Usenet: only parts of a file, only when you turn it on",
     "With Usenet on, AIOSubtitles also syncs files you play from Usenet. It reads only what it needs (the file's index "
     "and its subtitle track, usually a few MB; with the audio route, short parts of the sound of a file without a "
     "track) through a Usenet service in your AIOStreams that can stream part of a "
     "file: AIOStreams' own Usenet streaming, NzbDAV, AltMount, Easynews, or TorBox Usenet for releases TorBox already "
     "has. It never uses Stremio's NNTP streams, never makes TorBox download a release, and never searches or reads "
     "anything in the background. Off, a file you play from Usenet gets a synced copy only when your debrid service "
     "has the same file ready. NzbDAV and AltMount count only when AIOStreams' own proxy serves them. A file it "
     "doesn't find there isn't looked for again for a day, and an error from your indexer or Usenet service stops it "
     "until 00:00 UTC."),
    ("Only ready debrid files are read",
     "subsync reads a file only when your debrid service already has it ready (cached), never one the service would "
     "first have to download, and never one found in your debrid account's own download list. So subsync never adds a "
     "download to your account. Files played from plain torrents or web links get no synced copy. Usenet files are "
     "read only when you turn Usenet on (see \"Usenet: only parts of a file\")."),
    ("Only MKV and MP4 files",
     "subsync reads the subtitle tracks of MKV and MP4 files only, the formats nearly all streaming releases use. "
     "Other files (AVI, M2TS, fragmented MP4 and the like) keep their original subtitles, after one small read that "
     "shows the format."),
    ("A file that can't be reached is tried again later",
     "A file that can't be reached on its first read is tried once more 10 seconds later; if it still fails, it waits "
     "10 minutes, then 1, 6 and 24 hours, and then isn't read again. A Usenet file gets no second try: nothing more is "
     "looked for on Usenet until 00:00 UTC, and the file waits 1, 6 and 24 hours."),
    ("A copy is offered only after it passes the check",
     "Each synced copy is checked against the file itself before it is offered: at least 60 % of its lines must pair "
     "with lines of the file's own subtitle track (start within a second of one), at least 90 % of those must start "
     "within 200 ms (a fifth of a second) of theirs (95 % when snapped), with a typical error of 100 ms or less, "
     "spread over the whole film, or it must be clearly better than the original (SUBSYNC_BETTER). Where the "
     "track is in the subtitle's language and timing alone leaves doubt, the lines' words must agree too, except in "
     "Chinese and Japanese (SUBSYNC_TEXT_PAIRING); a copy fitted in sections for another cut must pass in every "
     "section (SUBSYNC_SECTIONS). Line ends are measured and recorded, but don't decide. Files without a subtitle "
     "track are checked against their speech instead (the audio route). A copy that fails is never offered: you get "
     "the original subtitles, as without subsync."),
    ("A language with no synced copy is listed with its original",
     "A language that has subtitles but no synced copy for a file is listed once, as \"<Language> (original)\": the "
     "subtitle, unchanged, that fit the file best when it was checked, or else the likeliest by its release name, and "
     "only if its text is in that language. It isn't synced; a synced copy, when there is one, is listed instead."),
    ("Subtitles are timed to the film's original language",
     "AIOSubtitles looks up each film's or series' original language by the title's id: at TMDB when a TMDB key is "
     "set, and at Wikidata (for anime, AniList). When the file has a subtitle track in that language, every subtitle, "
     "in any language, is checked and timed against it, because it follows the original speech: a plain track first, "
     "then a picture track (with SUBSYNC_IMAGE_REF on); one for the hearing impaired only for a language with no plain "
     "track of its own. A file with no subtitle track is fitted to its original-language sound first. Until the "
     "language is known, or when the file has no such track, each subtitle is checked against its own language's "
     "track as before."),
    ("At most four languages",
     "./setup syncs up to four languages. Each one is a separate sync for every file you play, with its own subtitle "
     "downloads and a few seconds of work, though the video file is read only once."),
    ("subsync's config starts nothing in the background",
     "subsync's own AIOStreams config has failover, preloading, pre-caching of the next episode, service wrap, "
     "cache-and-play and owned-file checks turned off, whatever your config uses. So reading a listed file opens that "
     "one file and nothing else, and subsync never starts a download. Your own config keeps its settings."),
    ("Nothing changes without your yes",
     "./setup shows each change to your AIOStreams config before saving it, saves a copy of your config first (in "
     "state/backups/), and changes nothing if you answer no. ./setup --restore FILE puts a saved copy back; "
     "./setup --uninstall takes subsync out again."),
    ("Links and keys stay private",
     "subsync's log never contains stream links, config addresses, keys or passwords, and the files in state/ that "
     "hold them can be read only by your user. The routes that make subsync work (its subtitle lists and status) "
     "answer only under a private key in their address, and each entry in its lists carries a code made from that key, "
     "so nobody who finds subsync's address can make it read files; only the synced files, those entries and a health "
     "check are open."),
    ("Public mode: your AIOStreams password is used once, unless you choose to keep it",
     "When you sign up or refresh, AIOSubtitles uses your AIOStreams password in that request and then forgets it, "
     "unless you tick the box that lets it keep the password: then it is kept encrypted in your record, never in a "
     "log, until you untick it. It needs it to read your config, to make a separate config for AIOSubtitles on your "
     "AIOStreams (without Real-Debrid, Usenet, AIOSubtitles itself or anything that starts work in the background, so "
     "opening a file opens only that file; with Usenet on, a second one for Usenet only) and, if you leave \"Add "
     "AIOSubtitles to my AIOStreams config\" ticked, to add AIOSubtitles to your own config. "
     "Without that separate config, a file that failed to open could make your AIOStreams move on to another source, "
     "Real-Debrid or a Usenet download among them."),
    ("Public mode: your AIOStreams is checked while you use it",
     "While you use AIOSubtitles, it checks your AIOStreams in the background, at most once a day by default "
     "(SUBSYNC_RECHECK_HOURS; 0 turns the check off): whether your config still exists (a question that needs no "
     "password) and whether its own config there still opens. It repairs a changed link by itself and tells you what "
     "only you can fix, on your settings page and with one \"AIOSubtitles: action needed\" entry in your subtitle "
     "lists. Without a kept password it can't see services or addons you add or remove, so press Refresh after such a "
     "change; with one, it rebuilds its configs from yours by itself, Real-Debrid always left out and Usenet kept to "
     "its Usenet config, and never changes your config."),
    ("Public mode: Real-Debrid is left out",
     f"If your AIOStreams uses Real-Debrid, AIOSubtitles leaves it out of its own config and never reads a Real-Debrid "
     f"file, because {REASON}. It syncs only files from your other services: a file you play from Real-Debrid gets a "
     "synced copy when another of your services has the same file ready. With Real-Debrid as your only debrid service, "
     "AIOSubtitles reads nothing for you: you get only the copies already synced for someone else who played the same "
     "release. With Usenet on, AIOSubtitles reads the files you play from Usenet for you, through your Usenet "
     "services; Real-Debrid stays out. Your own config and apps keep Real-Debrid as "
     "before, and your AIOStreams still searches it when you browse."),
    ("Public mode: a few files at once",
     "The service works on a set number of files at once for everyone (SUBSYNC_SYNCS_AT_ONCE), in the order people "
     "asked; two people playing the same file of a torrent never read it twice at once: the second gets the first "
     "one's work. A file's "
     "subtitle track is about 1 MB (a few MB when the track's lines are pictures, whose words AIOSubtitles reads on "
     "its own server and doesn't keep) and is synced in seconds. Files already synced for someone are served at once. "
     "When "
     "many people play new files together, the synced copy can come after your app has picked a subtitle; the next "
     "time you open the file, it is there."),
    ("Public mode: limits per person",
     "Each person can have a few files waiting and read a set number of new files and megabytes a day (UTC), by "
     "default 3 waiting, 200 files and 10,000 MB, and the whole service reads at most its own daily total; files already "
     "synced for someone don't count. Past a limit, you get only copies already synced for someone until the next day, "
     "and nobody else is affected. Sign-up attempts are limited per internet address, and the service takes a set "
     "number of people. Your settings page shows today's use."),
    ("Public mode: files without a subtitle track",
     "With the audio route on, a file with no usable subtitle track can be synced from its sound, as you choose on "
     "your settings page: off; after you pick its \"<Language> (AIOSubtitles)\" entry (the default); or, where the "
     "instance offers it, as soon as your app asks for its subtitles, if no synced copy exists and a subtitle exists "
     "to fit. Never through Real-Debrid. AIOSubtitles reads short parts of the sound through your own debrid service "
     "or Usenet, about 60–350 MB, at most the instance's audio limit, and works one to three minutes; your app gets "
     "the original at once, and the synced copy is there next time, for everyone who plays the file. It "
     "starts only when your day's data and audio syncs, and the service's data, likely have room; otherwise, or if it "
     "stops half-way, the file waits for another pick. The service runs a set number of these at once "
     "(SUBSYNC_AUDIO_AT_ONCE)."),
    ("Public mode: what is kept, and for how long",
     "AIOSubtitles keeps one record per person, encrypted so that it opens only with that person's personal link: the "
     "address of their AIOStreams, the id of their config, AIOSubtitles' own config there and its password (with "
     "Usenet on, its Usenet config's too), their languages and their answer for files with no subtitles of their own, "
     "their own TMDB key if they added one, and when their AIOStreams was last checked, plus how much was read for "
     "them each day. For up to 7 days after someone plays a file, the original subtitles listed for it in their "
     "languages are kept too (and, while it waits for their pick, its title, name and size), under a key that only "
     "their personal link leads to. Your AIOStreams password is kept only if you tick \"Keep my password\", in the "
     "same encrypted record, until you untick it. For each synced subtitle only its new timing (never its text), with "
     "the names and sizes of the files it fits, is shared and kept 180 days after last use, without who played them; "
     "logs name the titles worked on, never who asked. Your record is deleted when you delete it on your settings "
     "page, or 180 days after your last request by default (SUBSYNC_USER_KEEP_DAYS); backups the host keeps may hold "
     "it longer."),
    ("Public mode: the timing is shared, not the subtitles",
     "When someone already synced a subtitle for the file you play, AIOSubtitles reuses that work: it keeps only how "
     "the subtitle was re-timed, never its text. Your copy is made from the original subtitle, downloaded again for you "
     "from its site, with that timing, so no new check is needed. This follows the subtitle sites' rules, which don't "
     "allow one person's stored copy to be handed to others, and costs one small download. If the site changed or "
     "removed the subtitle since, you don't get that copy."),
    ("The same subtitle counts once",
     "When the same subtitle comes from two places, for example from an addon in your config and from a site "
     "AIOSubtitles searches itself, it is checked and listed once. Same means the same words at the same times: "
     "another text encoding, line ending or a credit line such as a site's address at the start or end doesn't make it "
     "a different subtitle, while another timing does. The copy kept is the first one tried, which is the one that "
     "knows most about your release. This saves time and keeps your subtitle list free of repeats."),
    ("Which subtitles are tried first",
     "For each language, AIOSubtitles tries first the subtitle your config lists first, then the ones made for your "
     "exact file (matched by its fingerprint or torrent, or with as many lines as the file's own track), then those "
     "of your release, then the rest. A subtitle that names the same edition as the file (Director's Cut, Extended, "
     "Theatrical, …) is tried before others of its kind; one that names another edition is tried last. Among equally "
     "likely ones, your config's addons come first, then OpenSubtitles, "
     "TheSubtitleDB, SubDL, SubSource, Gestdown and Wyzie. With no subtitle addon in your config, no subtitle has a "
     "place of its own as your first. A subtitle whose text isn't in the language it is listed under (judged by its "
     "letters and common words once it has about 300 words) is never synced, offered or served as that language."),
    ("A failing subtitle site is paused",
     "A site AIOSubtitles searches itself that fails three times within ten minutes (no answer, or an error) is skipped "
     "for ten minutes, then tried with one request: if it answers, it is used again; if not, it waits another ten "
     "minutes. A site that asks AIOSubtitles to slow down waits as long as it asks, at most an hour. A site that simply "
     "has no subtitles for a title isn't failing. Your other sources keep working meanwhile, so a broken site can't make "
     "your subtitles late."),
    ("Sites with limits give each file only a few subtitles",
     "Some sites allow only so many requests or downloads: SubDL 300 downloads a day for each internet address and "
     "2,000 searches a day for each key, SubSource 7,200 requests a day, Wyzie 1,000. AIOSubtitles counts them and takes "
     "at most three subtitles per file and language from SubDL, four from SubSource and two from Wyzie, the likeliest "
     "first. Near a site's limit, only subtitles made for your exact file or release are taken from it; at the limit "
     "it rests until its count starts again. On a service many people use, someone who already had their share of a "
     "site gets it only for those likeliest subtitles, so one person can't use up a site for everyone."),
]

# ------------------------------------------------------------------------------------------- ./setup's questions
# Usenet's explanation, with "{searches}" for its searches a day: the ./setup question gives the default of
# SUBSYNC_USENET_LISTINGS_PER_DAY, ./setup's screens and the public pages' Usenet box the install's own number
# (usenet_help, setup_help)
USENET_TEXT = (
    "Off by default. On: when a file you play isn't on your debrid service, AIOSubtitles looks for it through a "
    "second config it makes on your AIOStreams, with only your Usenet searches and services, and reads the few MB it "
    "needs (more for audio syncs). Recommended if you play from Usenet and your indexer's daily limit isn't "
    "tight. The cost: at most {searches} a day on each indexer (usually none, because your AIOStreams reuses "
    "the search you just made), fetching a release's NZB when your Usenet service doesn't have it yet, and some "
    "Usenet data per file (more than the few MB read, because Usenet sends whole pieces of about 0.7 MB). It helps "
    "you first, and everyone who plays the same release, because one sync serves all of them. Off: nothing on Usenet "
    "is searched or read.")


def usenet_help(per_day):
    """Usenet's explanation with an install's searches a day (SUBSYNC_USENET_LISTINGS_PER_DAY): "at most 10 extra
    searches a day", "at most 1 extra search a day"."""
    return USENET_TEXT.format(searches=f"{per_day} {plural(per_day, 'extra search', 'extra searches')}")


# The audio question's explanation, with "{opened}" for the files that may start as soon as they are opened, a day
# (SUBSYNC_USER_OPENED_PER_DAY, AUDIO_OPENED); the audio limit's, with "{wait}" for the seconds an app waits for a pick
# (SUBSYNC_PICK_WAIT_MS). The questions below give the defaults, ./setup's screens this install's values (setup_help).
AUDIO_TEXT = (
    "Some files carry no subtitle track, so subsync can time a subtitle to them only by listening to their speech: "
    "it reads short parts of the sound (about 60–350 MB a file, at most the audio limit you choose next) and keeps "
    "one processor busy for one to three minutes. When I pick the file (recommended with two or more processors): "
    "only after you choose the file's \"<Language> (Subsync)\" entry, so only files you watch are read. As soon as "
    "a file is opened: when your app asks for the file's subtitles, so files you only browse or that it prepares "
    "next are read too ({opened}). No: those files keep their original subtitles and subsync reads about 1 MB of "
    "each.")
AUDIO_OPENED = ("at most {n} a day", "none on this install: such files wait for a pick")     # (some, none)
AUDIO_BUDGET_TEXT = (
    "Asked only with the audio route on: the most it reads of one file, through your debrid service or Usenet: "
    "0.5, 1, 2 or 4 GB. Recommended: 1: it covers nearly every file with no subtitle track (web releases, MP4s, "
    "re-encodes); remuxes usually carry image subtitles and need no audio. Higher: very high-quality files such "
    "as 4K remuxes get synced too, but each can cost that much data and take longer than the {wait} seconds an app "
    "waits, so the copy comes the next time you open the file. Lower: less data on a metered connection; more "
    "high-bitrate files keep their originals. Smaller files are read only as far as needed. A change makes "
    "subsync judge again the files it kept as originals.")

# answers-file key -> (example value for docs/answers.example, explanation), in the order ./setup asks them, except
# AUDIO_BUDGET: setup asks it with the limits, after PUBLIC_ADDRESS; here it follows the audio and Usenet questions
QUESTIONS = {
    "ROUTE": (
        "yourself",
        "Install it for yourself (yourself, the default). This adds it to the AIOStreams you already run. Host a "
        "public instance (host). Runs here with its own sign-up page, so other people can use it with their own "
        "AIOStreams."),
    "AIOS_CONTAINER": (
        "",
        "Asked only when more than one AIOStreams runs in Docker on this machine: the one your apps use. subsync joins "
        "that container's Docker network and finds files through it, so the wrong one means it finds none of the files "
        "you play. Pick the one whose address your apps use."),
    "AIOS_URL": (
        "",
        "Asked only when setup can't find or can't reach AIOStreams running in Docker on this machine: the address of "
        "AIOStreams' configure page, without /stremio/..., like http://127.0.0.1:3000 or https://aio.example.com. "
        "subsync talks to AIOStreams at this address, so it must work from this machine."),
    "AIOS_CONFIG_ID": (
        "",
        "The ID of the AIOStreams config your apps use: a long code like 1a2b3c4d-…, shown on AIOStreams' configure "
        "page when you save or load the config, and part of its install link (…/stremio/<ID>/…). You can paste the "
        "whole install link, or its alias link (…/stremio/u/<name>/…): setup takes the ID out of it and checks that the "
        "config exists. subsync is added to this config, and subsync's own config is made from it."),
    "AIOS_CONFIG_PASSWORD": (
        "",
        "The password you chose when you saved that config. Setup uses it to read the config and save the change, and "
        "doesn't store it; subsync gets its own config with its own random password. Removing subsync and restoring a "
        "saved copy ask for it again. If you put it in an answers file, keep that file private."),
    "LANGUAGES": (
        lang_name(DEFAULT_LANGUAGE),
        "The subtitle languages to sync, up to four, the first one first; English if you choose none. In an answers "
        "file, names or codes separated by commas, like English, Spanish, ru. For each language and each file you "
        "play, subsync downloads that language's subtitles from your subtitle sources and the ones it searches itself, "
        "and checks them against the file. Each extra language adds those downloads and a few seconds of work per "
        "file; the video file is still read only once."),
    "AUDIO": ("no", AUDIO_TEXT.format(opened=AUDIO_OPENED[0].format(n=5))),
    "USENET": ("no", usenet_help(10)),
    "AUDIO_BUDGET": ("1", AUDIO_BUDGET_TEXT.format(wait=14)),
    "SUBDL_KEY": (
        "",
        "Optional: your free SubDL API key, from https://subdl.com/panel/api. With it, subsync also searches SubDL, "
        "which often has subtitles the other sources don't, so a copy that fits is more likely. Without it, SubDL isn't "
        "asked. Cost: one SubDL search for each file you play, plus a download per subtitle tried (at most three per "
        "file and language), counted against your key's own limit; the search sends the title's IMDb id. Kept only on this machine, in state/, readable only by your user."),
    "WYZIE_KEY": (
        "",
        "Optional: your free Wyzie Subs API key, from https://wyzie.io. With it, subsync also searches Wyzie Subs, "
        "which often has subtitles the other sources don't, so a copy that fits is more likely. Without it, Wyzie Subs "
        "isn't asked. Cost: one search per language for each file you play, plus a download per subtitle tried (at "
        "most two per file and language), counted against your key's own limit; the search sends the title's IMDb id. Kept only on this machine, in state/, "
        "readable only by your user."),
    "TMDB_KEY": (
        "",
        "Optional: a TMDB API key or API Read Access Token, free for non-commercial use (on themoviedb.org: your "
        "account's settings, API). With it, AIOSubtitles asks TMDB for each film's or series' original language, which "
        "TMDB knows for nearly every title, so more subtitles are timed to the file's track in that language. Without "
        "it, only Wikidata is asked, which knows about three films in four and half of all series; the rest are synced "
        "as if the language were unknown. Cost: one request per film or series every 6 months at most; it sends only "
        "the title's IMDb id. TMDB's answers are kept up to 6 months, as its terms allow. TMDB's logo and notice are "
        "shown; the key itself is never shown or logged."),
    "ADD_OPENSUBTITLES": (
        "yes",
        "Asked only when your config has no subtitle source, so AIOStreams gives subsync nothing to sync. Yes adds "
        "Stremio's official OpenSubtitles addon to your config: free, no account, it lists every language, and subsync "
        "syncs only the ones you picked. Recommended: yes. No: subsync works only from the sources it searches itself, "
        "and your apps get no original subtitles through AIOStreams. In Stremio, install your addon link once more "
        "afterwards so it starts asking for subtitles."),
    "PUBLIC_ADDRESS": (
        "",
        "The address your apps download synced subtitle files from. The suggestion sits behind the same web address as "
        "AIOStreams, like https://aio.example.com/subsync, which needs one rule in your web server (setup shows it), "
        "as does a name of its own, like https://subsync.example.com; on a home network, this machine's own port "
        "works, like http://192.168.1.5:8790. It must work from every device you watch on, outside your home too if "
        "you watch there; otherwise entries show but fail to load. Only synced files, the entries subsync lists and a "
        "health check are open at that address."),
    "CACHE": (
        "no",
        "AIOStreams keeps every subtitle answer for 5 minutes. Yes turns that off for subsync only, so a copy made "
        "while you watch shows the next time you open the file instead of up to 5 minutes later; other subtitle sources "
        "keep their cache. It needs AIOStreams' admin sign-in and changes that one setting on your AIOStreams server. "
        "Recommended: yes if you have the admin sign-in. Cost: AIOStreams asks subsync every time, a few milliseconds."),
    "AIOS_USERNAME": (
        "",
        "Your AIOStreams sign-in (the account of its web page), asked only when this AIOStreams needs one to save "
        "configs, or for the subtitle cache setting (then an admin account). Used during setup and not stored."),
    "AIOS_PASSWORD": (
        "",
        "The password of that AIOStreams account. Used during setup and not stored. If you put it in an answers file, "
        "keep that file private."),
    "CONFIRM": (
        "yes",
        "Saves the changes shown here: subsync added to your config (after a backup copy), and subsync's own config "
        "and its Usenet config created, updated or, when no longer used, deleted. Until then nothing is changed: go "
        "back to change an answer, or leave and run ./setup again any time. Removing subsync and restoring a saved "
        "copy ask their own questions before they change anything."),
    "TEST_READ": (
        "no",
        "A small test read at the end: subsync reads the start and the index of one ready file (usually under 1 MB), as "
        "it does for every file you play. Yes: you see now that reading works and whether that file has its own "
        "subtitle track; your debrid service opens the file, so it may show in your debrid account's list. No "
        "(default): the first file you play is the first read."),
    "HOST_ADDRESS": (
        "",
        "The web address of your public instance, with https, like https://subtitles.example.com: people open it to "
        "sign up, and their apps download synced files from it. Your web server sends it to this machine; setup shows "
        "the one rule it needs. It goes into every personal link, so choose one you will keep: changing it later "
        "breaks the links people already added to their AIOStreams."),
    "SUBSYNC_AUDIO_ROUTE": (
        "no",
        "Off by default. Off removes it completely: no one's audio is read and the pages don't offer it; such files "
        "keep their original subtitles. On: each person chooses on their settings page: off, when they pick a file's "
        "\"(AIOSubtitles)\" entry (the default), or as soon as a file is opened. Each sync reads about 60–350 MB of "
        "the file's sound through that person's own debrid service or Usenet and keeps about one processor busy for one "
        "to three minutes, within the limits you set next. Recommended: on, if your server has two or more "
        "processors; one sync serves everyone who plays that file. The reads count in each person's daily data and "
        "the service's daily total; nothing goes through Real-Debrid."),
    "REMOVE_OPENSUBTITLES": (
        "yes",
        "Asked when you remove subsync, if setup had added OpenSubtitles to your config. Yes takes it out again, "
        "leaving your config as it was before subsync; no keeps it, so your apps keep getting OpenSubtitles' subtitles."),
    "REMOVE_HERE_ONLY": (
        "no",
        "Asked when removing subsync, only when AIOStreams can't be reached or your config is gone from it. Yes "
        "removes subsync's container, its stored copies and setup's files here, and lists what stays on AIOStreams. No "
        "changes nothing."),
    # removing and restoring each ask their own yes, never CONFIRM's, so an answers file made for installing never
    # removes or restores anything
    "REMOVE": (
        "no",
        "Asked by ./setup --uninstall before anything is removed. Yes takes subsync out of your config, deletes its "
        "own config and removes its container with its stored copies; on a public instance, everyone's records go "
        "too, except in the database copies made before updates, which stay in state/backups/. No changes nothing. It "
        "is kept apart from CONFIRM, so an answers file made for installing never removes anything."),
    "RESTORE": (
        "no",
        "Asked by ./setup --restore FILE before the saved copy is put back into your config. Yes replaces your config "
        "with that copy, after saving the current one. No changes nothing."),
}

# ------------------------------------------------------------------------------------------- subsync's settings
# environment name -> (default as written in the environment, None when required; explanation), grouped for the README

_THRESHOLD = "Higher or lower than recommended trades how many copies are offered against how closely they fit."

SETUP_GROUP = "Written by ./setup"   # ./setup owns these lines of state/subsync.env; it keeps every other line

GROUPS = [
    (SETUP_GROUP, {
        "SUBSYNC_SOURCE_MANIFEST": (
            None,
            "The link of subsync's own AIOStreams config, which subsync finds files and subtitles through. ./setup "
            "makes that config from yours, without Usenet, Real-Debrid, subsync itself or anything that works in the "
            "background, and writes this. Required on your own install, except where Real-Debrid is your only debrid "
            "service and Usenet is on: then ./setup makes no such config and leaves this empty. Keep it private: "
            "anyone with it can list through that config."),
        "SUBSYNC_SOURCE_PASSWORD": (
            None,
            "The password of subsync's own config: random, made by ./setup, never yours. Before every request through "
            "that config, subsync reads it with this password and checks that it holds no way to Real-Debrid; when the "
            "check fails or the password is wrong, nothing is listed and the log says why. Required whenever "
            "SUBSYNC_SOURCE_MANIFEST is. Keep it private."),
        "SUBSYNC_PUBLIC_BASE": (
            None,
            "The address your apps download synced files from: your answer to ./setup's address question, like "
            "https://aio.example.com/subsync. subsync puts it in every entry it lists, so a wrong one makes entries "
            "show but fail to load. Required: subsync doesn't start without it."),
        "SUBSYNC_BASE_PATH": (
            "",
            "The path part of that address, like /subsync, when your web server passes it on unchanged; subsync removes "
            "it from incoming requests. ./setup sets it from the address, and leaves it empty when subsync has its own "
            "port."),
        "SUBSYNC_ADDON_KEY": (
            "",
            "A private key in the address of the routes that make subsync work: its manifest, its subtitle lists and "
            "its status. Without the key those routes answer \"not found\", so nobody who finds subsync's address can "
            "make it read files; synced files, /health and the entries of subsync's own lists stay open for your apps, "
            "and an entry's address can't be made up without the key. Made by ./setup; keep it private. Empty: no key "
            "and every route open, safe only where nobody else can reach subsync."),
        "SUBSYNC_LANGUAGES": (
            DEFAULT_LANGUAGE,
            "The languages to sync, as codes separated by commas, like eng,rus: ./setup writes your answer to its "
            "languages question. Each language is a separate sync for every file you play, with its own subtitle "
            "downloads and a few seconds of work; the video file is read only once."),
        "SUBSYNC_AUDIO_ROUTE": (
            "off",
            "Your answer to ./setup's audio question, on or off: whether files with no subtitle track of their own are "
            "synced from their speech, reading up to SUBSYNC_AUDIO_BUDGET_GB of audio each, after someone picks their "
            "\"(Subsync)\" entry or, with SUBSYNC_AUDIO_WHEN=open, as soon as they are opened; on a public instance each "
            "person chooses. Recommended: on if the server has two or more processors. Off: they keep their original "
            "subtitles and subsync reads about 1 MB per file. On a public instance each person's audio reads go "
            "through their own debrid service or Usenet and count in their daily data, and one sync serves everyone "
            "who plays the same file."),
        "SUBSYNC_AUDIO_WHEN": (
            "pick",
            "Pick or open: when a file with no subtitle track of its own starts its audio sync, with SUBSYNC_AUDIO_ROUTE "
            "on. pick (recommended): only after its \"(Subsync)\" entry is picked. open: as soon as an app asks for the "
            "file's subtitles, if no synced copy exists, a subtitle exists to fit and the day's data has room, at most "
            "SUBSYNC_USER_OPENED_PER_DAY a day; it reads 60–350 MB of files nobody may watch. A public instance ignores "
            "it: each person chooses on their settings page."),
        "SUBSYNC_USENET": (
            "off",
            "Off by default. On your own install: on, subsync also syncs files you play from Usenet, through a second "
            "config made from yours with only your Usenet searches and services; off, nothing on Usenet is searched or "
            "read. On a public instance: on lets each person turn Usenet on for themselves, with their own Usenet "
            "services and indexers through their own AIOStreams; nobody's Usenet is searched or read until they do, "
            "and this server holds no Usenet or indexer key."),
        "SUBSYNC_USENET_MANIFEST": (
            "",
            "The link of subsync's Usenet config, a second config ./setup makes from yours when you turn Usenet on, "
            "with only your Usenet searches and services and nothing that works in the background. subsync lists "
            "through it only when your debrid service doesn't have the file you play, and for every file when "
            "Real-Debrid is your only debrid service; then it also holds your subtitle sources. Keep it private."),
        "SUBSYNC_USENET_PASSWORD": (
            "",
            "The password of that config: random, made by ./setup, never yours, used as SUBSYNC_SOURCE_PASSWORD is. "
            "Keep it private."),
        "SUBSYNC_AUDIO_BUDGET_GB": (
            "1",
            "With the audio route on, the most audio subsync reads of one file, in GB: your answer to ./setup's audio "
            "limit question (0.5, 1, 2 or 4), written only while the audio route is on. Within it, high-bitrate files "
            "get shorter audio parts, or none when they would be too short to judge, and then keep their originals, as "
            "do MKV files without a seek index; a smaller file is read only as far as needed. Recommended: 1. Higher: "
            "very high-quality files such as 4K remuxes get copies too, at up to that much data each. Lower: less "
            "data, more such files keep their originals. Another number set here by hand works too; 0 removes the "
            "limit, which is not recommended. On a public instance it counts in each person's daily data "
            "(SUBSYNC_USER_DAILY_MB)."),
        "SUBSYNC_URL_REWRITE": (
            "",
            "Rewrites the start of stream links, like https://aio.example.com=http://aiostreams:3000, so subsync reads "
            "files from AIOStreams over Docker on this machine instead of out through your web server and back. "
            "./setup sets it when both run here and leaves it empty when they don't. A wrong rule makes every read fail, "
            "and files keep their original subtitles."),
        "SUBSYNC_SUBDL_KEY": ("", QUESTIONS["SUBDL_KEY"][1]),
        "SUBSYNC_WYZIE_KEY": ("", QUESTIONS["WYZIE_KEY"][1] + " A public instance doesn't start with it set."),
        "SUBSYNC_TMDB_KEY": ("", QUESTIONS["TMDB_KEY"][1]),
        "SUBSYNC_PORT": (
            "8790",
            "The port subsync listens on: inside its container, or on the machine itself when AIOStreams uses host "
            "networking. Leave it as it is: ./setup picks the port on this machine from your address: the address's "
            "own port (8791 in http://192.168.1.5:8791), or, for an address behind your web server, 8790 or the next "
            "free port (the rule setup shows names it)."),
        "SUBSYNC_BIND": (
            "0.0.0.0",
            "The address subsync listens on. ./setup sets 127.0.0.1 when AIOStreams uses host networking and your web "
            "server passes subsync's address on, so nothing reaches subsync except through your web server. Leave it "
            "as it is otherwise: in a container, Docker needs 0.0.0.0 to pass its port on."),
    }),
    ("Limits and safety", {
        "SUBSYNC_DAILY_BUDGET_GB": (
            "100",
            "The most subsync reads from files in one day (UTC), in GB received. When it's spent, reading stops until "
            "the next day and new files keep their original subtitles. 100 is far above normal use (about 1 MB per file, "
            "a few MB when the track's lines are pictures; up to the audio limit per file with the audio route, 1 GB "
            "by default), so it only catches something going wrong. Lower it if your debrid service or server has a "
            "tight traffic limit."),
        "SUBSYNC_USENET_LISTINGS_PER_DAY": (
            "10",
            "How many times a day AIOSubtitles may look for a Usenet file through your indexers (10 by default). Each "
            "look is one search per indexer in your config, usually answered from AIOStreams' own cache at no cost. "
            "Lower it if your indexer's daily limit is tight; 0 turns Usenet off. On a public instance this is per "
            "person, through each person's own indexers."),
        "SUBSYNC_INDEX_CAP_MB": (
            "32",
            "The most subsync reads of one file to find its subtitle tracks (the start, the index, and 64 KB at each "
            "end), in MB. Most files need about 1 MB; files with very many tracks need more. A file whose index is "
            "larger keeps its original subtitles and isn't read again until the cap is raised; an index over 32 MB (64 "
            "MB in an MP4 file) is never read, whatever the cap. Recommended: 32. Higher, up to 64: a few more MP4 "
            "files with very many tracks get copies, each costing that much more data."),
        "SUBSYNC_FILE_CAP_GB": (
            "6",
            "The most subsync reads of one file in all when the audio route works on it, in GB: an outer safety limit, "
            "since the audio limit (SUBSYNC_AUDIO_BUDGET_GB, at most 4 GB from ./setup) normally stops first. "
            "Recommended: 6. 0 removes it, which is not recommended."),
        "SUBSYNC_USER_AUDIO_PER_DAY": (
            "20",
            "Audio syncs one person may start a day (UTC), picked or opened. Each reads about 60–350 MB of a file's "
            "sound and keeps a processor busy for one to three minutes, and its copy then serves everyone who plays "
            "that file. 20 by default; recommended: keep it, so the processors are shared fairly while the daily data "
            "limit bounds the reads. 0 stops new audio syncs, takes the audio question off the pages and keeps the "
            "copies already made. Past it, that person's files wait for a pick the next day."),
        "SUBSYNC_USER_OPENED_PER_DAY": (
            "5",
            "Of those, how many may start as soon as a file is opened, for people who chose that (UTC day). 5 by "
            "default; recommended: keep it low, since apps also ask for files people only browse or that they prepare "
            "next. 0 removes that answer from the pages; people who had chosen it then sync when they pick."),
        "SUBSYNC_403_BACKOFF_S": (
            "900",
            "After a debrid service refuses a read (HTTP 403 or 429, usually a per-account limit), subsync stops all "
            "reads for this many seconds, doubling with each refusal in a row up to 6 hours; the refused file itself "
            "waits 1, 6, then 24 hours. Recommended: 900 (15 minutes). Shorter: copies resume sooner, but subsync keeps "
            "knocking on a limit your own playback needs too."),
        "SUBSYNC_RETRY_STOPPED_S": (
            "600",
            "A file whose work stopped because the daily limit was spent, or reads were paused after a refusal, is tried "
            "again after this many seconds. Recommended: 600 (10 minutes). Shorter only means more checks of a pause "
            "that is still on."),
        "SUBSYNC_READ_TIMEOUT_S": (
            "60",
            "How long one read of a file may wait for an answer, in seconds, before it counts as failed; a read that "
            "keeps going counts as failed too once it has taken this long plus one second for every 64 KB it asked "
            "for. A file's first read is then tried once more 10 seconds later, and a failed file is tried again 10 "
            "minutes, 1, 6 and 24 hours later (a Usenet file: no second try, then 1, 6 and 24 hours). Recommended: 60. "
            "Shorter: slow services fail more often."),
        "SUBSYNC_SIZE_SLACK_MB": (
            "5.5",
            "How far a file's listed size may be from its real size and still be taken for the same file, in MB; "
            "listings round sizes to 0.01 GiB, up to 5.1 MB off. Recommended: 5.5. Lower: rounded listings stop "
            "matching and those files get no copy. Higher: another file of the same name could be taken, though its "
            "own hash still catches that."),
        "SUBSYNC_STORE_CAP_MB": (
            "2048",
            "Disk space for synced copies, in MB; past it, the copies served longest ago are deleted and made again "
            "when needed, which costs a new read. The store's database (what was checked, and the files' details) "
            "isn't counted: it stays small because what nobody used for 180 days is deleted. 2048 holds about 20,000 "
            "copies. Lower it on a small disk."),
        "SUBSYNC_SKIP_ADDONS": (
            "",
            "Names of AIOStreams addons, separated by commas, whose listed files subsync never reads; empty by default. "
            "Use it for an addon whose files you don't want read; those files get no synced copy."),
    }),
    ("Which subtitles are tried", {
        "SUBSYNC_EXTRA_SOURCES": (
            "osv3,osv3plus,tsdb,gestdown",
            "Subtitle sites AIOSubtitles searches itself, beside the subtitle addons in your AIOStreams config: osv3 "
            "(OpenSubtitles through Stremio's own addon), osv3plus (OpenSubtitles V3+), tsdb (TheSubtitleDB) and "
            "gestdown (Gestdown, series only); none needs a key. Recommended: all four: they often have a subtitle made "
            "from your exact release, which fits best. Remove one to stop asking it. Each search sends the title's IMDb "
            "id, season and episode and your languages; the OpenSubtitles ones also get the file's fingerprint (hash), "
            "size and name, as your player sends them, and TheSubtitleDB the torrent's hash and the file's name. A site "
            "that is already an addon in your config isn't asked twice. Other names are ignored: SubDL, SubSource and "
            "Wyzie Subs have their own key settings."),
        "SUBSYNC_SOURCE_TIMEOUT_S": (
            "5",
            "How long AIOSubtitles waits for each subtitle site it searches itself, in seconds. A site that answers "
            "later, within a minute, is left out for this play and its answer is kept for the next one, so a slow site "
            "never makes your first subtitle late. Recommended: 5. Longer: slow sites count more often, but your first "
            "subtitle can come later. A site that fails three times in ten minutes (an answer that takes more than a "
            "minute counts as failing) is paused for ten minutes, then tried again."),
        "SUBSYNC_SKIP_CONFIG_SOURCES": (
            "on",
            "On: a subtitle site that is already an addon in your AIOStreams config (for example OpenSubtitles or "
            "SubDL) is searched only once, through your config and with your settings for it. Recommended: on: half the "
            "requests to that site and no repeated subtitles. Off: AIOSubtitles also asks it itself, with its own "
            "settings, which can find subtitles your settings for it leave out, at the cost of a second search."),
        "SUBSYNC_SUBSOURCE_KEY": (
            "",
            "Optional: a free SubSource API key (create one under API in your account on subsource.net). With it, "
            "AIOSubtitles also searches SubSource, which has many subtitles the other sites don't, so a copy that fits "
            "is more likely. Without it, SubSource isn't asked. Cost: two requests per title and one per subtitle "
            "tried, counted against the key's limit (60 a minute, 7,200 a day for a free key); at most four subtitles "
            "per file and language come from it. The search sends the title's IMDb id and season. The key is sent in a "
            "header, never shown or logged."),
        "SUBSYNC_TSDB_KEY": (
            "",
            "Optional: a free TheSubtitleDB key (sign up on thesubtitledb.org). Searching TheSubtitleDB needs no key; "
            "the key raises its downloads from 1,000 an hour to 100,000 a day. Recommended: leave it empty for your own "
            "install, which never comes near that limit; set it for a service many people use. The key is sent in a "
            "header, never in an address, and is never shown or logged."),
        "SUBSYNC_SKIP_AI": (
            "on",
            "On: subtitles marked as AI-made, machine-translated or forced (covering only foreign-language parts) are "
            "never tried. Recommended: on: they read badly or cover only part of the film. Off: they can become synced "
            "copies like any other."),
        "SUBSYNC_RANK_BY_RELEASE": (
            "on",
            "On: subtitles made for this exact release (by its file hash or release name) are tried first. "
            "Recommended: on: they usually fit as they are, so your copy is ready sooner. Off: subtitles are tried in "
            "your sources' order."),
        "SUBSYNC_BEST_FIT": (
            "on",
            "On: subsync judges every subtitle it tries and lists the best-fitting copy first. Off: it lists the first "
            "copy that passes, in your sources' order, and stops there. Recommended: on: the first entry you see fits "
            "best. Cost: every try is made, a few more downloads and seconds per file."),
        "SUBSYNC_MAIN_ALTERNATIVES": (
            "3",
            "With best fit on, how many synced copies of your first language are listed, best first. Recommended: 3, "
            "so you can switch when the first has a problem the check can't see, like a poor translation. 1: a shorter "
            "list."),
        "SUBSYNC_GATE_TRIES": (
            "20",
            "With best fit on, how many subtitles per language subsync checks as they are (a fraction of a second "
            "each, no re-timing), looking for one that already fits. Recommended: 20. Higher: more downloads per file "
            "for a slightly better chance of an exact fit."),
        "SUBSYNC_MAX_TRIES": (
            "6",
            "How many subtitles per language and file subsync re-times with alass (the timing tool) before it stops; "
            "each try is a download and a few seconds of work. Recommended: 6. Higher: a better chance on hard files, "
            "at more time and downloads. Lower: faster, and more files keep their originals."),
    }),
    ("How copies are made and checked", {
        "SUBSYNC_FAST_PATH": (
            "on",
            "On: each subtitle is first checked as it is, and one that already fits is listed at once without "
            "re-timing. Recommended: on: faster, and a fitting original is never altered. Off: every subtitle is "
            "re-timed first."),
        "SUBSYNC_SHIFT_PATH": (
            "on",
            "On: before full re-timing, subsync tries moving the whole subtitle by one steady amount, the common case "
            "of a film a second early or late. Recommended: on: fast and exact when it applies. Off: full re-timing "
            "every time, slower."),
        "SUBSYNC_TEXT_PAIRING": (
            "on",
            "On: when your subtitle and the file's own subtitle track share a language and timing alone can't settle "
            "it, subsync reads the words of up to 120 of the track's lines, once per file and language (up to 240 KB "
            "of text; on an image track, with a reader installed for the language, each line's picture, up to 64 KB), "
            "and pairs your subtitle's lines with those that say the same thing, even where lines are split or joined "
            "differently. A copy whose words disagree with its timing isn't offered. Chinese and Japanese, written "
            "without spaces between words, are paired by timing alone, and nothing more is read for them. Words are "
            "kept only while the file is worked on. Recommended: on. Off: lines are paired by timing alone, nothing "
            "extra is read, and a copy only words could refuse may be offered."),
        "SUBSYNC_SECTIONS": (
            "on",
            "On: a subtitle made for a slightly different cut of the film (a few seconds added or cut here and there, "
            "other ads or recaps) is fitted in up to 8 sections, each moved by its own amount and each checked on its "
            "own; a copy is offered only when every section passes and the whole copy does too, and a subtitle that "
            "lacks a scene of your file is still never offered. Recommended: on. Off: the whole film is moved by one "
            "amount, then by the general re-timing. Nothing extra is read either way."),
        "SUBSYNC_SNAP": (
            "on",
            "On: after re-timing, each line moves to start exactly where the file's own matching line starts, wherever "
            "that match is clear. Recommended: on: lines appear right on cue. Off: lines keep their author's timing "
            "inside the re-timed blocks."),
        "SUBSYNC_SNAP_WITHIN": (
            "0.95",
            "The share of a snapped copy's paired lines that must start within 200 ms of the file's track "
            "(0.95 = 95 %). Snapped lines look better to the check than they are, so they need more than the 90 % other "
            "copies need. Recommended: 0.95. Lower: more copies offered, a few slightly off."),
        "SUBSYNC_BETTER": (
            "on",
            "On: a copy that misses the 90 % bar is still offered when it is clearly better than the original: its "
            "typical error is 200 ms or less and at most half the original's, at least 85 % of its lines pair with the "
            "file's lines, and no part of the film is missing. Recommended: on: such a copy is a plain improvement. "
            "Off: only copies that meet the bar; the rest keep their originals."),
        "SUBSYNC_BETTER_PAIRED": (
            "0.85",
            "For \"clearly better\": the share of the copy's lines that must pair with a line of the file's track "
            "(0.85 = 85 %). Recommended: 0.85. " + _THRESHOLD),
        "SUBSYNC_BETTER_MEDIAN_MS": (
            "200",
            "For \"clearly better\": the copy's typical error, the median distance of its line starts from the file's, "
            "must be at most this many ms. Recommended: 200. " + _THRESHOLD),
        "SUBSYNC_BETTER_RATIO": (
            "0.5",
            "For \"clearly better\": the copy's typical error must be at most this share of the original's (0.5 = "
            "half). Recommended: 0.5: a smaller gain isn't worth replacing the original. " + _THRESHOLD),
        "SUBSYNC_BETTER_CROSS_MEDIAN_MS": (
            "300",
            "For \"clearly better\" when the only track to check against is in another language (a translation checked "
            "against the English track): the typical error must be at most this many ms. Recommended: 300, looser than "
            "200 because translations split and merge lines. " + _THRESHOLD),
        "SUBSYNC_BETTER_CROSS_PAIRED": (
            "0.75",
            "For \"clearly better\" against another language's track: the share of the copy's lines that must pair "
            "(0.75 = 75 %). Recommended: 0.75. " + _THRESHOLD),
        "SUBSYNC_ORIGINAL_LANGUAGE": (
            "on",
            "On (recommended): AIOSubtitles looks up each film's or series' original language, so subtitles are timed "
            "to the file's track in that language, which follows the original speech. The lookup sends only the "
            "title's id, from this server, once per title (the answer is kept with no time limit; one from TMDB up to "
            "6 months), and never delays a sync: a file read before the answer comes is checked again on its next "
            "play. Off: nothing is looked up, every subtitle is "
            "checked against its own language's track (else SUBSYNC_FALLBACK_REFERENCE's) as before, and the answers "
            "already kept are deleted."),
        "SUBSYNC_ORIGINAL_LANGUAGES": (
            "",
            "Optional: titles whose original language you set yourself, which then is never looked up, as id=language "
            "pairs separated by commas, like tt1375666=en,mal:5114=ja: an IMDb id, or mal: or kitsu: and the number "
            "for anime known only by MyAnimeList or Kitsu (two- or three-letter codes). Use it when a lookup gives a "
            "wrong language for a title you know. Empty (recommended): every title is looked up. A wrong entry times "
            "that title's subtitles to the wrong track, so fewer of its copies pass the check."),
        "SUBSYNC_FALLBACK_REFERENCE": (
            "eng",
            "When a file has no track in a subtitle's language, the languages whose track it is checked against first, "
            "as codes separated by commas. Recommended: eng: English tracks are the most common and the most complete. "
            "Empty: such subtitles are checked against no other language's track."),
        "SUBSYNC_PREFER_PLAIN_TRACK": (
            "on",
            "On: copies are checked against the file's plain subtitle track rather than its SDH track (the one with "
            "sound descriptions like [door slams], which has extra lines). Recommended: on: a cleaner comparison and "
            "fewer copies refused for lines the SDH track adds."),
        "SUBSYNC_IMAGE_REF": (
            "on",
            "On: files whose only subtitles are images (PGS or VobSub, common in Blu-ray rips) are checked against those "
            "images' timings. Recommended: on. Off: such files keep their originals, unless the audio route is on."),
        "SUBSYNC_GATE_SPLITS": (
            "on",
            "On: when a subtitle splits one of the file's lines in two, the second half counts as matched. Recommended: "
            "on: otherwise good copies that split lines fail the check and you get the original."),
        "SUBSYNC_LIST_IN_SYNC": (
            "on",
            "On: an original further down the list that already fits the file is listed as a synced copy too. "
            "Recommended: on: you can see that it fits. Off: it stays listed only under its original name."),
        "SUBSYNC_PARALLEL_LANGUAGES": (
            "on",
            "On: your languages are synced at the same time. Recommended: on: copies are ready sooner. Off: one after "
            "another, less work at once on a small server."),
        "SUBSYNC_ANCHORED_GUESS": (
            "on",
            "With several languages, while a file waits for the audio route: your other languages are shifted onto the "
            "first language's best guess, so all of them start in step. Recommended: on. Off: they stay as they are "
            "until their own copies are ready."),
    }),
    ("When the audio route works", {
        "SUBSYNC_AUDIO_ON_PICK": (
            "on",
            "The setting SUBSYNC_AUDIO_WHEN replaced, still read while that one isn't set: on means pick, off means "
            "open. Recommended: leave it out and set SUBSYNC_AUDIO_WHEN instead. A public instance ignores both: each "
            "person chooses on their settings page."),
        "SUBSYNC_PICK_WAIT_MS": (
            "14000",
            "How long your app's download of a \"(Subsync)\" entry waits for the synced copy, in ms, before it gets the "
            "original instead. Recommended: 14000 (AIOMetadata gives up after 15 s). Lower: the original comes sooner "
            "and more often."),
        "SUBSYNC_PENDING_ENTRY": (
            "off",
            "On: instead of \"(Subsync)\" entries, a \"(synced)\" entry is listed for a file still being worked on, "
            "served as the original until the copy is ready, and subtitle lists wait up to SUBSYNC_PENDING_WAIT_MS. "
            "Recommended: off: the \"(Subsync)\" entry already gives the copy once it is ready, and lists don't wait."),
        "SUBSYNC_PENDING_WAIT_MS": (
            "1000",
            "With pending entries on, how long a subtitle list waits for a file's work before answering, and how long a "
            "pending entry waits for its copy, in ms. Recommended: 1000. Longer: more copies on the first try, slower "
            "lists."),
        "SUBSYNC_AUDIO_LEAD_MS": (
            "190",
            "Audio route: in good releases a subtitle appears about 190 ms before the speech starts, so copies timed "
            "from speech are moved this much earlier. Recommended: 190. 0: lines appear exactly as speech starts, a "
            "little late to the eye."),
        "SUBSYNC_AUDIO_EXTRA_WINDOWS": (
            "12",
            "Audio route: when the first check against the speech is a close call (just short of the bar, or passing on "
            "few lines), up to this many more 30-second pieces of the sound are read to decide, within your audio "
            "limit (each piece is about half a percent of a two-hour film's file: about 10 MB of a 2 GB film), and the "
            "file is judged again on all of them. Recommended: 12. 0: the first check decides and nothing more is read. "
            "More: surer verdicts on quiet or long films, more data read on those films only, and their copies take "
            "a few seconds longer."),
        "SUBSYNC_AUDIO_BETTER": (
            "on",
            "Audio route: on, a copy that misses the standard bar against the speech is still offered when it is "
            "clearly closer to the speech than the original (the five rules below). Recommended: on. Off: only copies "
            "that meet the standard bar; more of these files keep their originals."),
        "SUBSYNC_AUDIO_BETTER_MATCHED": (
            "25",
            "Audio route, \"clearly closer\": at least this many of the copy's lines must match speech. Recommended: "
            "25. " + _THRESHOLD),
        "SUBSYNC_AUDIO_BETTER_MISS": (
            "0.30",
            "Audio route, \"clearly closer\": at most this share of the copy's lines may match no speech (0.30 = 30 %). "
            "Recommended: 0.30. " + _THRESHOLD),
        "SUBSYNC_AUDIO_BETTER_MEDIAN_MS": (
            "150",
            "Audio route, \"clearly closer\": the copy's typical error from the speech must be at most this many ms. "
            "Recommended: 150. " + _THRESHOLD),
        "SUBSYNC_AUDIO_BETTER_WITHIN": (
            "0.55",
            "Audio route, \"clearly closer\": at least this share of the lines that match speech must start within "
            "200 ms of it (0.55 = 55 %), lower than for a subtitle track because speech detection is less exact. "
            "Recommended: 0.55. " + _THRESHOLD),
        "SUBSYNC_AUDIO_BETTER_MIN_SHIFT_S": (
            "1.0",
            "Audio route, \"clearly closer\": the copy must have moved the lines by at least this many seconds; a "
            "smaller change isn't worth replacing the original. Recommended: 1.0."),
    }),
    ("Programs and addresses", {
        "SUBSYNC_DATA": (
            "/data",
            "Where subsync keeps its database and synced copies inside its container, on a Docker volume that survives "
            "updates. Leave it."),
        "SUBSYNC_ALASS": (
            "alass",
            "The alass program subsync re-times subtitles with, built into its image. Leave it."),
        "SUBSYNC_FFMPEG": (
            "ffmpeg",
            "The ffmpeg program that decodes short audio parts for the audio route, built into its image. Leave it."),
        "SUBSYNC_VAD_MODEL": (
            "",
            "The file of the speech-detection model (Silero VAD) the audio route uses; empty takes the one in the image. "
            "Leave it."),
        "SUBSYNC_TSDB_URL": (
            "https://api.thesubtitledb.org",
            "The address of TheSubtitleDB's API, a source subsync searches itself. Change it only if the service "
            "moves."),
        "SUBSYNC_GESTDOWN_URL": (
            "https://api.gestdown.info",
            "The address of Gestdown's API (series subtitles), a source subsync searches itself. Change it only if the "
            "service moves."),
        "SUBSYNC_CINEMETA_URL": (
            "https://v3-cinemeta.strem.io",
            "The address of Cinemeta, Stremio's catalogue, which subsync asks for a series' TheTVDB number before it "
            "searches Gestdown. Change it only if it moves."),
        "SUBSYNC_TMDB_URL": (
            "https://api.themoviedb.org/3",
            "The address of TMDB's API, which AIOSubtitles asks for a film's or series' original language when a TMDB "
            "key is set. Change it only if it moves."),
        "SUBSYNC_WIKIDATA_URL": (
            "https://query.wikidata.org/sparql",
            "The address of Wikidata's query service, which AIOSubtitles asks for a film's or series' original "
            "language. Change it only if it moves; Wikidata has announced new addresses for 2026–2027."),
        "SUBSYNC_KITSU_URL": (
            "https://kitsu.app/api/edge",
            "The address of Kitsu's API, which AIOSubtitles asks for an anime's MyAnimeList number when the anime is "
            "known only by its Kitsu number, to look up where it comes from. Change it only if it moves."),
        "SUBSYNC_ANILIST_URL": (
            "https://graphql.anilist.co",
            "The address of AniList's API, which AIOSubtitles asks which country an anime comes from: its original "
            "language is Japanese unless AniList names China, Taiwan, Hong Kong or Korea. Change it only if it moves."),
        "SUBSYNC_OSV3PLUS_URL": (
            "https://opensubtitles.stremio.homes",
            "The address of OpenSubtitles V3+, a source subsync searches itself. Change it only if the service moves."),
        "SUBSYNC_OSV3_URL": (
            "https://opensubtitles-v3.strem.io",
            "The address of Stremio's own OpenSubtitles addon, a source AIOSubtitles searches itself. Change it only if "
            "it moves."),
        "SUBSYNC_SUBSOURCE_URL": (
            "https://api.subsource.net/api/v1",
            "The address of SubSource's API, used only with a SubSource key. Change it only if the service moves."),
        "SUBSYNC_SUBDL_URL": (
            "https://api.subdl.com",
            "The address of SubDL's search API, used only with a SubDL key. Change it only if the service moves."),
        "SUBSYNC_SUBDL_DOWNLOAD_URL": (
            "https://dl.subdl.com",
            "The address SubDL's subtitle files are downloaded from, used only with a SubDL key. Change it only if the "
            "service moves."),
        "SUBSYNC_WYZIE_URL": (
            "https://sub.wyzie.io",
            "The address of Wyzie Subs' API, used only with a Wyzie key. Change it only if the service moves."),
    }),
    ("Public mode (one service for many people)", {
        "SUBSYNC_TMDB_PERSONAL": (
            "off",
            "For a public instance with no TMDB key of its own. On: each person may add their own free TMDB key on "
            "their settings page; it is used to look up the original language of the films and series they play, and "
            "the answers are shared with everyone. Without one, their titles are looked up on Wikidata only, unless "
            "someone else's key found them already. Off: the service's own key, if any, is used for everyone; with "
            "SUBSYNC_TMDB_KEY set it must stay off, or the service doesn't start. Each person's key is stored "
            "encrypted in their record, opens only with their personal link, and is never shown or logged."),
        "SUBSYNC_PUBLIC": (
            "off",
            "Public mode: one AIOSubtitles service for many people, like public.aiosubtitles.com. Each person signs up "
            "on its web page with their AIOStreams link and password and gets a personal addon link; subsync then "
            "works through a separate sync config it makes on their AIOStreams. Leave it off for your own install; "
            "setup turns it on when you host a public instance. On, the service needs SUBSYNC_SECRET_KEY and "
            "SUBSYNC_PUBLIC_BASE, lets each person choose their audio syncs, and shares how each copy was synced (its "
            "timing, never its text) between everyone playing the same file, so the more people use it, the more files "
            "are ready when someone presses play. It holds other people's links, so it needs a server you keep up to "
            "date and backed up."),
        "SUBSYNC_SECRET_KEY": (
            "",
            "The service's secret, at least 32 random bytes written as base64url; setup makes one and keeps it in "
            "state/subsync.env, readable only by your user (by hand: what python3 -c \"import secrets; "
            "print(secrets.token_urlsafe(32))\" prints). It encrypts each person's record together with their personal "
            "link, so neither the database nor this secret alone opens a record. Required in public mode; never logged "
            "or shown. Keep that file in your backups: if the secret is lost or changed, everyone has to sign up again."),
        "SUBSYNC_PUBLIC_INVITE": (
            "",
            "When set, the sign-up page asks for this code, so only people you gave it to can sign up, for example "
            "while you and your testers try the service. Capitals don't matter. Empty lets anyone sign up. It "
            "doesn't affect people who already signed up. Recommended while testing; remove it to open the service."),
        "SUBSYNC_MAX_USERS": (
            "200",
            "How many people can be signed up at once; past it the sign-up page says the service is full. Each person "
            "costs a little disk and, while they play, a worker, so the limit keeps the server within what it can "
            "carry. Raise it only after watching memory, processor use and the daily read total for a while."),
        "SUBSYNC_SIGNUPS_PER_HOUR": (
            "5",
            "Sign-up attempts allowed per internet address per hour, counted before anything is fetched; an IPv6 "
            "address counts with the rest of its /64 block. Each attempt makes the service contact that person's "
            "AIOStreams several times, so this stops one address from flooding the service or someone else's "
            "AIOStreams. 5 is plenty for someone who mistypes a password; lower is stricter, higher helps many people "
            "behind one shared address. One address can also sign up only that many times in 24 hours."),
        "SUBSYNC_USER_FILES_PER_DAY": (
            "200",
            "New files read per person per day (UTC). Files already in the shared store don't count, and neither does "
            "serving a copy. Past it, that person gets only copies the store already has until the next day; nobody "
            "else is affected. 200 is more than anyone watches in a day, with room for the files apps open without "
            "playing them; it keeps one person, or a leaked link, from using up the service's daily reads."),
        "SUBSYNC_USER_DAILY_MB": (
            "10000",
            "Megabytes read per person per day (UTC) through their own sync config. Reading a file's subtitle track "
            "takes about 1 MB, a few MB when the track's lines are pictures; 10,000 is 200 files at the most one file's "
            "index may take (32 MB) with room to spare, so those reads never stop someone before the file limit. A "
            "file the audio route syncs from its sound takes about 60–350 MB, so 10,000 MB covers at least 28 a day at "
            "that size, and about 10 of high-bitrate files that read the whole audio limit (1 GB by default); "
            "such a sync starts only when what is left of the day likely covers it, so it rarely stops half-way; if it "
            "does, the file waits for another pick. Past the limit, that person gets only copies already in the store "
            "until the next day."),
        "SUBSYNC_MAX_LANGUAGES": (
            "4",
            "How many languages each person may choose, from 1 to 4. Each language is a separate sync for every file "
            "they play: its own subtitle downloads and a few seconds of work, while the file itself is read once. "
            "Recommended: 4, the most the pages offer; lower it only when the server can't keep up. A lower limit "
            "applies when people next save their languages; until then they keep the ones they have."),
        "SUBSYNC_USER_QUEUE": (
            "3",
            "Files one person may have waiting at once. A request past it is answered with what the shared store "
            "already has and starts nothing. Everyone's files wait for the same few places (SUBSYNC_SYNCS_AT_ONCE), so "
            "a longer queue for one person delays everyone else; 3 covers someone opening a few episodes in a row."),
        "SUBSYNC_ACTIVE_WORKERS": (
            "100",
            "How many people can have a worker (the part that finds and syncs their files) running at once. A worker "
            "with nothing to do stops after a while (30 minutes by default). Past the limit, a person's requests get "
            "only copies already in the shared store until a worker frees up. Each worker takes about 2.5 MB of "
            "memory, plus up to 32 MB for the subtitles it downloaded in the last 10 minutes, so 100 take from about "
            "250 MB to about 3.5 GB; keep the limit within what the server's memory allows. Re-timing needs more, for "
            "a few seconds at a time: one alass run takes about 0.4 GB, up to about 1.2 GB for a 3-hour film, and at "
            "most one runs per processor, so leave about 1 GB per processor for it too."),
        "SUBSYNC_WORKER_IDLE_MIN": (
            "30",
            "Minutes without a request before a person's worker stops and their opened record leaves memory. Shorter "
            "frees memory sooner and keeps opened records in memory for less time; longer saves reopening the record "
            "when someone pauses between episodes. Reopening is quick, so 30 is a fair middle."),
        "SUBSYNC_USER_KEEP_DAYS": (
            "180",
            "Days without a single request before a person's record is deleted (a whole number from 1). Their sync "
            "config can't be removed from their AIOStreams then, because opening the record needs their personal link; "
            "the privacy page says so. Shorter keeps less data about people who left; longer spares people who come "
            "back after a long break from signing up again."),
        # what the pages say about the host's own setup: empty, they say it in words true of any host
        "SUBSYNC_CONTACT": (
            "",
            "Where people using this public instance can ask questions, like https://github.com/you/project/issues or "
            "mailto:you@example.com: the pages' Questions link. Empty: the pages show no Questions link."),
        "SUBSYNC_FRONT_PROXY": (
            "",
            "The name of the service in front of this instance that sees every request, like Cloudflare, for the "
            "privacy page. Empty: the privacy page says the web server in front of it is run by whoever hosts it."),
        "SUBSYNC_BACKUP_DAYS": (
            "",
            "How many days your backups keep people's encrypted records, like 30, for the privacy and settings pages. "
            "Empty: they say the host's backups may keep a deleted record longer."),
        "SUBSYNC_TRUSTED_PROXY": (
            "127.0.0.1",
            "The addresses of the web server in front of subsync, comma-separated. Only requests from these may say, "
            "in their X-Forwarded-For header, which address they came from, and the sign-up limit per address counts "
            "that one. Set it to your web server's address as subsync sees it, or to gateway when the web server "
            "reaches subsync through a port Docker publishes: subsync then trusts its Docker network's gateway, found "
            "at start, with no fixed network to set up. A wrong value makes every visitor count as one address, or "
            "lets visitors choose their own."),
        "SUBSYNC_MAX_CONNECTIONS": (
            "256",
            "Requests handled at once. Each one takes a thread and some memory, so past it the service answers \"busy, "
            "try again\" (HTTP 503) at once instead of running out of memory. Apps ask for subtitles again on their "
            "own. 256 leaves room for 200 people's apps asking at once; raise it together with the memory the service "
            "may use."),
        "SUBSYNC_RECHECK_HOURS": (
            "24",
            "How often, at most, each person's AIOStreams is checked again while they use the service, in hours; 0 "
            "turns the check off. A check asks their AIOStreams two things (does their config still exist, does "
            "AIOSubtitles' own config there still open), repairs a changed link, and tells the person on their "
            "settings page and in their subtitle lists what only they can fix; for people who let AIOSubtitles keep "
            "their password it also rebuilds AIOSubtitles' config from theirs. Shorter notices changes sooner, at more "
            "calls to every AIOStreams; people who don't use the service aren't checked. With 0, the pages don't offer "
            "to keep the password, since nothing would use it."),
        "SUBSYNC_SYNCS_AT_ONCE": (
            "auto",
            "How many files the service works on at once for everyone, or auto for one per processor it may use. "
            "Recommended: auto, since one sync keeps about one processor busy. More lets people who play new files at "
            "the same time wait less, at more processor use and more requests to each AIOStreams at once; one public "
            "AIOStreams shared by many people limits how many requests one address may make, so give a number when the "
            "processors are many and that AIOStreams is shared. A torrent's file is never read twice at once: whoever "
            "plays it second waits for the first one's work and reads nothing."),
        "SUBSYNC_AUDIO_AT_ONCE": (
            "auto",
            "Audio syncs running at the same time, for everyone together. auto (the default, recommended) is half of "
            "SUBSYNC_SYNCS_AT_ONCE, at least one, so ordinary files never wait behind them; with one file at a time "
            "(SUBSYNC_SYNCS_AT_ONCE 1, or auto on one processor), they wait while an audio sync runs. A higher number "
            "finishes picked files sooner when many come together but makes every other file wait longer; it can't be "
            "more than SUBSYNC_SYNCS_AT_ONCE. A self-hosted install syncs one at a time whatever it says."),
    }),
]

SETTINGS = {name: entry for _, group in GROUPS for name, entry in group.items()}
WRITTEN_BY_SETUP = tuple(dict(GROUPS)[SETUP_GROUP])


def setup_help(key, settings=None):
    """A ./setup question's explanation (a limit's: its setting's) as ./setup shows it. The audio question's, the
    audio limit's and Usenet's state a setting (the files opened a day, the seconds an app waits for a pick, the Usenet
    searches a day): with `settings`, subsync's as state/subsync.env starts it, they state this install's value;
    without, its default, as QUESTIONS has them for README and docs/answers.example."""
    if settings is not None:
        if key == "AUDIO":
            opened = settings.user_opened_per_day
            return AUDIO_TEXT.format(opened=AUDIO_OPENED[0].format(n=opened) if opened else AUDIO_OPENED[1])
        if key == "AUDIO_BUDGET":
            return AUDIO_BUDGET_TEXT.format(wait=f"{settings.pick_wait:g}")
        if key == "USENET":
            return usenet_help(settings.usenet_listings_per_day)
    return (QUESTIONS.get(key) or SETTINGS[key])[1]


# ------------------------------------------------------------------------------------------- public mode's page texts

# (title, text), as PUBLIC_STORE_ONLY; the settings page's notice after a refresh gives it as one sentence
PUBLIC_REAL_DEBRID_WARNING = (
    "Real-Debrid is left out",
    f"AIOSubtitles never uses it, because {REASON}. It syncs only files from your other services, so a file you play "
    "from Real-Debrid gets a synced copy only when another of your services has the same file ready.")
PUBLIC_REAL_DEBRID_NOTICE = ": ".join(PUBLIC_REAL_DEBRID_WARNING)

# A setup whose only debrid service is Real-Debrid: stored copies only; the note ends with
# PUBLIC_STORE_ONLY_USENET where the instance lets people turn Usenet on (store_only_note).
PUBLIC_STORE_ONLY = (
    "Stored copies only",
    f"Real-Debrid is the only debrid service in your AIOStreams config, and AIOSubtitles never uses it, because {REASON}. "
    "So it reads none of your files: for each file you play, it offers the synced copies already made for someone who "
    "played the same release, when there are any. To get your own synced copies, add another service, such as TorBox, "
    "to your config, then press Refresh on your settings page.")
PUBLIC_STORE_ONLY_USENET = ("With Usenet on, AIOSubtitles reads the files you play from Usenet for you, through your "
                            "Usenet services; Real-Debrid stays out.")


def store_only_note(usenet):
    """The stored-copies note, (title, text): its Usenet sentence only where the instance offers Usenet (`usenet`)."""
    title, text = PUBLIC_STORE_ONLY
    return title, (text + " " + PUBLIC_STORE_ONLY_USENET if usenet else text)


PUBLIC_PASSWORD_TEXT = (
    "Your AIOStreams password, used now for two things: to add AIOSubtitles to your AIOStreams config (if you leave "
    "\"Add AIOSubtitles to my AIOStreams config\" ticked, on the last screen), and to make a separate AIOStreams "
    "config for AIOSubtitles without Real-Debrid, Usenet or automatic fallback to other sources{usenet_second}, so "
    "opening a file opens only that file. It is not stored{keep_below}. You'll be asked again only if you refresh.")

# The project's own Questions link, on ./setup's pages (a public instance's pages show SUBSYNC_CONTACT's)
CONTACT = f"{PROJECT}/issues"

# The public pages' words on Usenet and on keeping the password that hold only where the instance offers them, each
# (where it does, where it doesn't), by the "{…}" they fill in (instance_words). Usenet where people may turn it on
# (Settings.usenet_offered): a record made while it was on keeps its Usenet config until it is next rebuilt,
# but the pages describe the usual case.
USENET_WORDS = {
    "usenet_second": (" (with Usenet on, a second one for Usenet only)", ""),       # the sign-up's password
    "usenet_kept": (" Usenet goes only into AIOSubtitles' Usenet config,", ""),     # "Keep my password"
    "usenet_audio": (" (or Usenet, if you turned it on)", ""),                       # the audio question
    "usenet_or": (" or Usenet", ""),                                                 # the audio route's intro, privacy
    "usenet_read": (", or from your Usenet service when you turn Usenet on", ""),   # privacy: what is read
    "usenet_password": (" (and its Usenet config's, with Usenet on)", ""),          # privacy: what is stored
    "usenet_held": (                                                                 # privacy: what is stored
        "AIOSubtitles' own config holds your debrid keys, as your own config does (its Usenet config your Usenet and "
        "indexer keys), and the service can open them while your requests come in.",
        "AIOSubtitles' own config holds your debrid keys, as your own config does, and the service can open it while "
        "your requests come in."),
}
# Keeping the password where the pages offer it (Settings.keeps_passwords), and then the check's read of the
# config ("{every}": how often it runs). Without the offer, a password kept before the host turned the check
# off stays until the person refreshes or presses "Stop keeping my password", which shows only to them.
KEEP_WORDS = {
    "keep_below": (" unless you tick \"Keep my password\" below", ""),              # the sign-up's password
    "keep_tick": (", unless you tick \"Keep my password\"", ""),                    # the note above Sign up
    "keep_ticked": (" unless \"Keep my password\" is ticked", ""),                  # Refresh's password
    "keep_read": (", and, if you let AIOSubtitles keep your password, again {every} while you use the service",
                  ""),                                                               # privacy: what is read
    "keep_stored": (                                                                 # privacy: what is stored
        "Your AIOStreams password is stored only if you tick \"Keep my password\", in the same encrypted record, until "
        "you untick it",
        "This instance doesn't keep your AIOStreams password; one kept earlier stays until you refresh or press \"Stop "
        "keeping my password\""),
}

# one explanation for each field and button of the public pages; "{every}" (how often the check runs), "{backups}"
# (how long the host's backups keep what is deleted) and USENET_WORDS' and KEEP_WORDS' slots take the instance's words
# (field_help)
PUBLIC_FIELDS = {
    "link": (
        "The link of your AIOStreams config, the one your apps use: on AIOStreams' configure page, copy the install "
        "link (it ends in /manifest.json). AIOSubtitles reads your config through it once, now, and makes a separate "
        "config next to it on your AIOStreams. Your AIOStreams must be reachable from the internet over https. Keep "
        "the link private: anyone with it can use your AIOStreams config."),
    "password": PUBLIC_PASSWORD_TEXT,
    # "{limit}": the instance's language limit, "{choices}": LANGUAGES_CHOICES' words (languages_help)
    "languages": (
        "The languages you want synced subtitles in, up to {limit}, like English, Spanish. Each one is a separate sync "
        "for every file you play, so fewer is faster. {choices} You can change them later; a change applies from the "
        "next file you play."),
    "add_addon": (
        "Adds AIOSubtitles to your AIOStreams config, before your other subtitle sources, so your apps show its "
        "synced subtitles first (many apps show only a few per language). Recommended. Unticked, nothing in your own "
        "config changes and you add the link yourself as a custom addon."),
    # "{names}": the addons this instance takes out (clean_up_help)
    "clean_up": (
        "Takes out of your AIOStreams config the subtitle addons AIOSubtitles now searches itself: {names}. You then "
        "get their subtitles once, through AIOSubtitles: synced where one fits, the original otherwise, instead of the "
        "same ones twice. Your other subtitle addons stay. The cost: "
        "while AIOSubtitles is down, these subtitles are missing too, until it is back. Unticked, your config keeps "
        "them and nothing is removed. It works only together with adding AIOSubtitles; to undo it, add the addons "
        "again on AIOStreams' configure page."),
    "usenet": USENET_TEXT,      # the installer question's text, with the instance's number (usenet_help)
    # The answer for files with no subtitles of their own, with the instance's audio limit ("{at_most}", none
    # without one) and daily count (audio_help); each answer's own words are in PUBLIC_AUDIO_ANSWERS
    "audio": (
        "Some files carry no subtitle track, so a subtitle can only be timed to them by listening to their speech: "
        "AIOSubtitles reads short parts of the file's sound through your own debrid service{usenet_audio}, about "
        "60–350 MB a file{at_most}, and works one to three minutes; the synced copy then serves everyone "
        "who plays the same file, you included next time. These syncs count in your daily data, at most {per_day} a "
        "day."),
    "invite": "The code you were given. AIOSubtitles is open only to invited people for now.",
    "tmdb_key": (
        "Optional: your own free TMDB API key (on themoviedb.org: your account's settings, API). AIOSubtitles uses it to "
        "look up the original language of the films and series you play, so their subtitles are timed to the file's "
        "track in that language; it sends only the title's IMDb id, and the answers are shared with everyone. Without "
        "it, your titles are looked up on Wikidata only (for anime, AniList and Kitsu). It is stored encrypted in your "
        "record, opens only with your personal link, and is never shown; Remove key deletes it at once ({backups})."),
    "refresh": (
        "Your AIOStreams password, used now to read your config again and rebuild AIOSubtitles' own config from it, for "
        "example after you added or removed a service, to make that config again if it is gone, and to put "
        "AIOSubtitles back in your config if it is missing. It is not stored{keep_ticked}."),
    "new_link": (
        "Optional: the link of another AIOStreams config, if you moved to a new one. AIOSubtitles reads that config "
        "with the password above, follows it from now on, and moves its own config there if it is on another "
        "AIOStreams; your personal link stays the same. Empty: AIOSubtitles keeps following the config you signed up "
        "with."),
    "keep_password": (
        "Off by default. Ticked: AIOSubtitles keeps your AIOStreams password, encrypted so that it opens only with your "
        "personal link, and {every} while you use it rebuilds its own config from yours, so services and addons you "
        "add or remove are followed without Refresh; Real-Debrid stays out,{usenet_kept} and your own config is never "
        "changed. The cost: that password can change your whole "
        "AIOStreams config, all your keys included, "
        "so if AIOSubtitles' server were broken into while you use it, it could be read. Unticked: it is used once and "
        "forgotten. Untick and refresh, or press \"Stop keeping my password\", to delete it at once; {backups}."),
    "forget_password": (
        "Deletes the AIOStreams password AIOSubtitles kept, at once ({backups}); nothing else changes. AIOSubtitles "
        "then stops following changes to your config by itself, and you press Refresh after you change it."),
    "delete": (
        "Deletes everything AIOSubtitles keeps about you ({backups}) and removes its own config from your AIOStreams; "
        "your personal link stops working at once. Optional: with your AIOStreams password it also takes itself out of "
        "your AIOStreams config; without it, remove the AIOSubtitles addon there yourself. The password is used once "
        "and not stored. How subtitles were synced stays in the shared store: it belongs to files, not to you."),
    # the same for a person with stored copies only, who has no config of AIOSubtitles' own to remove
    "delete_stored": (
        "Deletes everything AIOSubtitles keeps about you ({backups}); your personal link stops working at once. "
        "Optional: with your AIOStreams password it also takes itself out of your AIOStreams config; without it, "
        "remove the AIOSubtitles addon there yourself. The password is used once and not stored. How subtitles were "
        "synced stays in the shared store: it belongs to files, not to you."),
}

# Usenet on the public pages: why a ticked box made no Usenet config, the settings
# row, and the box's explanation with the instance's own number of searches
USENET_NO_SERVICE = ("Usenet stays off: your AIOStreams has no Usenet service AIOSubtitles can read (it reads Usenet "
                     "files through services that stream part of a file; not Stremio's NNTP streams).")
USENET_WHY = {
    "proxy": ("Usenet stays off: your Usenet service gives addresses on your home network, which AIOSubtitles can't "
              "reach from the internet. Turn on AIOStreams' proxy for it, or leave Usenet off."),
    "nntp": USENET_NO_SERVICE,
    "none": USENET_NO_SERVICE,
}
# ./setup's own reasons: a self-hosted subsync usually shares the Usenet service's network, so for it the reason is that
# without AIOStreams' proxy those links need the WebDAV password (aiosconfig.usenet_route)
SETUP_USENET_WHY = dict(USENET_WHY, proxy=("Usenet stays off: subsync reads NzbDAV and AltMount only through "
                                           "AIOStreams' own proxy. Turn on AIOStreams' proxy for it, or leave Usenet "
                                           "off."))


# What each answer for files with no subtitles of their own does, under the answer ("{opened}": the instance's
# SUBSYNC_USER_OPENED_PER_DAY); the pages offer "open" only while that is above 0
PUBLIC_AUDIO_ANSWERS = {
    "off": ("Such files keep their original subtitles, you still get copies others made, and nothing of their sound is "
            "read for you."),
    "pick": ("It starts only when you choose the file's \"<Language> (AIOSubtitles)\" entry in your app, so only files "
             "you watch are read."),
    "open": ("It starts when your app asks for the file's subtitles, before you choose anything, if no synced copy "
             "exists yet, a subtitle exists to fit and your day's data has room; apps also ask for files you only open "
             "or that they prepare next, so it can read files you never watch (at most {opened} a day)."),
}


def audio_offered(settings):
    """Whether the public pages ask the question for files with no subtitles of their own and describe the audio
    route: with it on and audio syncs allowed (SUBSYNC_USER_AUDIO_PER_DAY above 0). With 0 they treat it as off; the
    answers people saved stay in their records for when it is raised again."""
    return settings is not None and settings.audio_route and settings.user_audio_per_day > 0


def audio_help(settings):
    """The explanation of the answer for files with no subtitles of their own, with the instance's audio limit (no
    "at most" without one) and the audio syncs a person may start a day."""
    limit = audio_limit_words(settings)
    return PUBLIC_FIELDS["audio"].format(at_most=f" and at most {limit}" if limit else "",
                                         per_day=settings.user_audio_per_day, **instance_words(settings))


def audio_answer_help(answer, settings):
    """What one answer ("off", "pick" or "open") does, with the opened syncs a person may start a day."""
    return PUBLIC_AUDIO_ANSWERS[answer].format(opened=settings.user_opened_per_day)


# The subtitle addons the sign-up's clean-up may take out, by AIOStreams preset (srclimits.CLEANABLE_PRESETS)
CLEAN_UP_NAMES = {"opensubtitles": "OpenSubtitles", "opensubtitles-v3-plus": "OpenSubtitles V3+", "subdl": "SubDL",
                  "subsource": "SubSource"}


def clean_up_help(presets):
    """The clean-up box's explanation, naming the addons this instance takes out (`presets`: extra.cleanable_presets),
    "OpenSubtitles, OpenSubtitles V3+, SubDL and SubSource" where it asks all four."""
    *first, last = [CLEAN_UP_NAMES[p] for p in presets]
    return PUBLIC_FIELDS["clean_up"].format(names=f"{', '.join(first)} and {last}" if first else last)


def usenet_status(on, names, used, per_day):
    """The settings row: "Usenet: on, through NzbDAV (2 of 10 searches used today)", "(0 of 1 search used today)" /
    "Usenet: off"."""
    if not on:
        return "Usenet: off"
    return f"Usenet: on, through {names} ({used} of {per_day} {plural(per_day, 'search', 'searches')} used today)"


def recheck_every(settings):
    """How often the check runs while a person uses the service (SUBSYNC_RECHECK_HOURS), as the pages say it: "about
    once a day" at 24 hours, "about every hour" at 1, "about every 6 hours", "about every 2 days"."""
    hours = settings.recheck_hours
    if hours == 1:
        return "about every hour"
    if hours == 24:
        return "about once a day"
    if hours % 24 == 0:
        days = int(hours // 24)
        return f"about every {days} {plural(days, 'day')}"
    return f"about every {hours:g} {plural(hours, 'hour')}"


# How long the host's backups keep what a person deletes (SUBSYNC_BACKUP_DAYS), in the words of each text that says
# it, (with the instance's days, without them): a field's explanation, "What is stored" and "Who else sees your
# requests" on the privacy page
BACKUP_WORDS = {
    "field": ("the encrypted backups keep it up to {days} more", "the host's encrypted backups may keep it longer"),
    "stored": ("and up to {days} more in the encrypted backups", "and possibly longer in the host's encrypted backups"),
    "kept": ("Backups of the stored data are kept for up to {days}.",
             "Whoever hosts this instance decides whether backups of the stored data are made and how long they are "
             "kept."),
}


def backup_words(settings, text):
    """How long the instance's backups keep a record, in `text`'s words (BACKUP_WORDS): with SUBSYNC_BACKUP_DAYS its
    days, "up to 30 days more"; without, that the host's backups may keep it longer."""
    days = settings.backup_days
    with_days, without = BACKUP_WORDS[text]
    return with_days.format(days=f"{days} {plural(days, 'day')}") if days else without


def instance_words(settings):
    """The words of the public pages that follow what the instance offers, by the "{…}" they fill in: how often the
    check runs ("{every}"), and USENET_WORDS and KEEP_WORDS, each pair's first words where it lets people turn Usenet
    on (Settings.usenet_offered) or keep their password (Settings.keeps_passwords), else its second."""
    every = recheck_every(settings)
    words = {slot: pair[not settings.usenet_offered] for slot, pair in USENET_WORDS.items()}
    words.update((slot, pair[not settings.keeps_passwords].format(every=every)) for slot, pair in KEEP_WORDS.items())
    return dict(words, every=every)


def field_help(name, settings):
    """A field's explanation (PUBLIC_FIELDS) with the instance's words (instance_words), and how long its backups keep
    what is deleted ("{backups}")."""
    return PUBLIC_FIELDS[name].format(backups=backup_words(settings, "field"), **instance_words(settings))


def public_text(name, settings):
    """One of PUBLIC_TEXTS with the instance's words, as field_help fills a field's (the note above Sign up, the note
    after deleting)."""
    return PUBLIC_TEXTS[name].format(backups=backup_words(settings, "field"), **instance_words(settings))


NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four"}
LANGUAGES_ONE = (
    "The language you want synced subtitles in, like English. {choices} You can change it later; a change applies "
    "from the next file you play.")
# The synced choices the first language gets ("{choices}" above), by the languages allowed (several, or one): (up to
# {n} of them, just one)
LANGUAGES_CHOICES = {
    "several": ("The first one gets up to {n} synced choices, the others one each.", "Each gets one synced choice."),
    "one": ("It gets up to {n} synced choices for every file you play.",
            "It gets one synced choice for every file you play."),
}


def synced_choices(settings):
    """The synced copies listed for the first language: SUBSYNC_MAIN_ALTERNATIVES with best fit on, else one (as the
    addon lists them)."""
    return settings.alternatives if settings.best_fit else 1


def languages_help(settings):
    """The languages explanation with the instance's language limit (SUBSYNC_MAX_LANGUAGES, 1 to 4) and the synced
    choices the first language gets (synced_choices), each number in words up to four; its own text for one
    language."""
    limit, choices = settings.max_languages, synced_choices(settings)
    up_to, just_one = LANGUAGES_CHOICES["one" if limit == 1 else "several"]
    words = up_to.format(n=NUMBER_WORDS.get(choices, choices)) if choices > 1 else just_one
    if limit == 1:
        return LANGUAGES_ONE.format(choices=words)
    return PUBLIC_FIELDS["languages"].format(limit=NUMBER_WORDS[limit], choices=words)


# the other texts of the public pages
PUBLIC_TEXTS = {
    # a person's own TMDB key on the settings page, when the instance asks each person for one
    "tmdb_saved": "Your TMDB key is saved. It is used for the next title looked up for you.",
    "tmdb_removed": "Your TMDB key is deleted.",
    "tmdb_refused": ("TMDB didn't accept this key. Copy it again from themoviedb.org (your account's settings, API) and "
                     "paste it here."),
    "tmdb_unreachable": "TMDB didn't answer, so the key wasn't saved. Try again in a few minutes.",
    # the landing intro's two paragraphs (intro_parts)
    "intro": (
        "AIOSubtitles gives you subtitles that are in sync with the file you play. It works through your AIOStreams: "
        "for each file you play, it reads that file's own subtitle track (about 1 MB; a few MB when the track's lines "
        "are pictures, whose words AIOSubtitles reads on its own server and doesn't keep) through a separate config it "
        "makes on your AIOStreams, and offers subtitles from your addons and the sites it searches itself, re-timed to "
        "fit."),
    "intro_shared": ("A file synced once is ready for everyone who plays it: its timing is kept and applied to the "
                     "subtitle, downloaded fresh for each person."),
    "signed_up": (
        "Keep this link: it is the only key to your settings, and nobody, AIOSubtitles included, can recover it. If you "
        "lose it, sign up again. Keep it private too: anyone with it can change your settings or delete your data."),
    "not_added": (
        "AIOSubtitles couldn't add itself to your AIOStreams config, so nothing there was changed."),
    "deleted": "Your data is deleted ({backups}), and your personal link no longer works.",       # public_text
    # AIOSubtitles' configs a delete couldn't remove, named by those left: its own config, its Usenet
    # config, or both
    "sync_left": (
        "AIOSubtitles couldn't remove its own config from your AIOStreams. It stays there unused and holds the same "
        "debrid keys as your config; whoever runs your AIOStreams can delete it."),
    "usenet_left": (
        "AIOSubtitles couldn't remove its Usenet config from your AIOStreams. It stays there unused and holds the same "
        "Usenet and indexer keys as your config; whoever runs your AIOStreams can delete it."),
    "configs_left": (
        "AIOSubtitles couldn't remove its two configs from your AIOStreams. They stay there unused and hold the same "
        "debrid, Usenet and indexer keys as your config; whoever runs your AIOStreams can delete them."),
    "addon_left": (
        "Remove the AIOSubtitles addon from your AIOStreams config yourself (Addons, then delete it and save); its "
        "link no longer works."),
    "tries": "Each try counts: {attempts} per address in an hour.",     # tries_text
    "kept_note": "For your safety, the link, password and invite code aren't kept after an error: enter them again.",
    "password_once": "Your password is used for this sign-up only and is not stored{keep_tick}.",     # public_text
    "copied": ("AIOSubtitles' config on your AIOStreams was copied from yours {when}. After you add or remove a service or "
               "addon in AIOStreams, press Refresh."),
    "copied_auto": ("AIOSubtitles' config on your AIOStreams was copied from yours {when}. AIOSubtitles keeps your "
                    "password and follows changes to your config by itself, {every} while you use it."),
    "forgot": ("Your kept password is deleted. AIOSubtitles no longer follows your config by itself: press Refresh after "
               "you change it."),
    "notice_where": ("Open your AIOSubtitles settings page: your personal link, ending in configure instead of "
                     "manifest.json."),
    "made_config": ("Made AIOSubtitles' own config on your AIOStreams, without Real-Debrid, Usenet or automatic "
                    "fallback to other sources."),
    # Usenet
    "usenet_made": ("Usenet is on: AIOSubtitles made a second config on your AIOStreams with only your Usenet searches "
                    "and services."),
    # with no debrid service AIOSubtitles may read, the Usenet config is its only one, with the subtitle sources too
    "usenet_only_made": ("Usenet is on: AIOSubtitles made its own config on your AIOStreams with only your Usenet "
                         "searches, Usenet services and subtitle sources."),
    "usenet_paused": ("Usenet is paused until 00:00 UTC: your indexer or Usenet service answered with an error, so "
                      "AIOSubtitles stopped asking it today. Nothing else is affected."),
    "languages_chosen": "Languages: {names}.",
    # an earlier sign-up of the same config, whose entry in it this one took, deleted
    "earlier_deleted": ("Your earlier sign-up of this config is deleted, with its own config on your AIOStreams; its "
                        "personal link no longer works."),
    "next": ("Play something in your app. For each file, AIOSubtitles offers synced copies of your subtitles in your "
             "app's subtitle list. A file nobody has played before takes a few seconds; if your app has already picked "
             "a subtitle by then, the synced copy is there the next time you open the file."),
    "settings_link": ("It is your personal link, ending in configure instead of manifest.json. Bookmark it to come "
                      "back."),
    "settings_lead": ("This page opens with your personal link. Keep the link private: anyone with it can change these "
                      "settings or delete your data."),
    "reset": "Resets at 00:00 UTC, {when}. Files that already have a synced copy don't count.",
    "limit_reached": "Until 00:00 UTC you get only copies already synced for someone; nobody else is affected.",
    "languages_saved": "Languages saved: {names}. They apply from the next file you play.",
    "audio_saved": "Saved for files with no subtitles of their own: {answer}. It applies from the next file you open.",
    "refreshed": "Refreshed: AIOSubtitles' config on your AIOStreams now matches yours.",
    "to_stored": ("Refreshed: Real-Debrid is now the only debrid service in your config, so AIOSubtitles removed its own "
                  "config from your AIOStreams and offers stored copies only."),
    "refresh_manual": ("AIOSubtitles' config on your AIOStreams now matches yours, but AIOSubtitles couldn't put itself "
                       "back in your own config."),
    "undone": "This can't be undone.",
    "not_found": ("If this was your personal link, its data may have been deleted, or the link was cut short: copy the "
                  "whole link again."),
    "went_wrong": "AIOSubtitles ran into a problem on its side. Try again in a few minutes.",   # an unexpected failure
}


def tries_text(per_hour):
    """The start page's note on the sign-up attempts an address may make in an hour (SUBSYNC_SIGNUPS_PER_HOUR)."""
    return PUBLIC_TEXTS["tries"].format(attempts=f"{per_hour} {plural(per_hour, 'sign-up attempt')}")


# The privacy page in four parts, (heading, text), filled in by privacy_parts with the instance's words: "{audio}" the
# audio route's on what is read, "{asked}" the sites it searches and the lookups it makes (PRIVACY_SITES,
# PRIVACY_CINEMETA, PRIVACY_LOOKUP), "{audio_answer}" and "{tmdb}" a person's answer for files with no subtitles of
# their own and their own TMDB key among what is stored, "{backups}" and "{backups_kept}" how long the host's backups
# keep a record (BACKUP_WORDS), "{front}" who sees every request (PRIVACY_FRONT), "{keep}" SUBSYNC_USER_KEEP_DAYS, and
# USENET_WORDS' and KEEP_WORDS' slots what the instance offers (instance_words).
PRIVACY_PARTS = (
    ("What is read",
     "your AIOStreams config, once at sign-up and when you refresh{keep_read}; then, for each file you play, its "
     "subtitle track (about 1 MB; a few MB when the track's lines are pictures, whose words AIOSubtitles reads on its "
     "own server and doesn't keep){audio} and the subtitles your AIOStreams lists, through AIOSubtitles' own config on "
     "your AIOStreams{usenet_read}.{asked}"),
    ("What is stored",
     "one record, encrypted so that it opens only with your personal link (your AIOStreams address, AIOSubtitles' own "
     "config there and its password{usenet_password}, your config's id, your languages, {audio_answer}{tmdb}and when "
     "your AIOStreams was last checked), and how much was read for you each day. For up to 7 days after you play a "
     "file, the original subtitles listed for it in your languages are kept too (and, while it waits for your pick, "
     "its title, name and size), under a key that only your personal link leads to. {usenet_held} {keep_stored} "
     "({backups}). For each synced subtitle only "
     "its new timing (never its text), with the names and sizes of the files it fits, is shared and kept 180 days "
     "after last use, without who played them. Logs name the titles worked on, never who asked."),
    ("Who else sees your requests",
     "{front} The subtitle links your app gets don't carry your personal link: their key opens only your subtitles, "
     "never your settings. AIOSubtitles' own log notes the titles it works on and when, never who asked, your internet "
     "address or your links. {backups_kept}"),
    ("How long it is kept",
     "until you delete your data on your settings page, or {keep} after your last request; then AIOSubtitles' own "
     "config can't be removed from your AIOStreams, because opening your record needs your link."),
)
# The sites AIOSubtitles searches itself, by source (extra.asked_sources): "{sites}" names those the instance asks, in
# the order of the rule "Which subtitles are tried first"; Cinemeta is asked only to find a series on Gestdown
PRIVACY_SITES = ("AIOSubtitles also searches subtitle sites itself ({sites}): they get the title's IMDb id, season and "
                 "episode, the languages, and the file's name, size and hashes, from AIOSubtitles' server, never your "
                 "internet address.")
SITE_NAMES = {"osv3": "OpenSubtitles", "osv3plus": "OpenSubtitles", "tsdb": "TheSubtitleDB", "subdl": "SubDL",
              "subsource": "SubSource", "gestdown": "Gestdown", "wyzie": "Wyzie"}
PRIVACY_CINEMETA = "For series, it first asks Cinemeta for TheTVDB's number by the title's id alone."
# Who sees every request, with the service in front of the instance (SUBSYNC_FRONT_PROXY), without it
PRIVACY_FRONT = ("AIOSubtitles runs behind {proxy}, which passes every request on and so sees your internet address "
                 "and the links you open, your personal link included.",
                 "The web server in front of AIOSubtitles passes every request on and so sees your internet address "
                 "and the links you open, your personal link included; whoever hosts this instance runs it.")
PRIVACY_AUDIO_STORED = "your answer for files with no subtitles of their own, "   # in "What is stored", where asked


class State(NamedTuple):
    """What the daily check (recheck.py) found, as the settings page and the "action needed" entry say it."""
    title: str
    text: str                  # "{detail}": the plain reason a rebuild couldn't be made, or AIOStreams' (state_text)
    person_must_act: bool      # only the person can fix it


PUBLIC_STATES = {
    "sync_gone": State(
        "AIOSubtitles' config on your AIOStreams is gone",
        "AIOSubtitles can no longer open the separate config it made on your AIOStreams, so it can't sync new files for "
        "you. Press Refresh with your AIOStreams password: it makes that config again.", True),
    "main_gone": State(
        "The AIOStreams config you signed up with is gone",
        "Your AIOStreams no longer has the config you signed up with. If you moved to a new config, press Refresh with "
        "its link and password: AIOSubtitles then follows it, and your personal link stays the same.", True),
    "password_refused": State(
        "Your kept password no longer works",
        "AIOSubtitles couldn't open your AIOStreams config with the password it kept, so it deleted that password and "
        "stopped following your config by itself. If you changed your password, press Refresh with the new one, and "
        "tick \"Keep my password\" to let it keep the new one.", True),
    "cannot_rebuild": State(
        "Your AIOStreams config can't be followed as it is",
        "AIOSubtitles tried to follow a change in your AIOStreams config and couldn't: {detail} Until that changes, it "
        "keeps using the config it made before.", True),
    "reads_refused": State(
        "Your debrid service refused AIOSubtitles' reads",
        "Your debrid service refused AIOSubtitles' last reads, so it pauses and tries again later. If you changed your "
        "debrid key in AIOStreams, press Refresh so AIOSubtitles' config gets the new one.", True),
    "unreachable": State(
        "AIOSubtitles couldn't reach your AIOStreams",
        "The last three checks couldn't reach your AIOStreams from the internet. While it can't be reached, AIOSubtitles "
        "can't sync new files for you; nothing needs doing once it is back.", False),
    # AIOStreams refused the config AIOSubtitles made from theirs (as words it on sign-up and Refresh);
    # three checks in a row that failed, not each by not reaching it
    "config_refused": State(
        "Your AIOStreams didn't accept AIOSubtitles' new config",
        "AIOSubtitles tried to follow a change in your AIOStreams config, and your AIOStreams didn't accept the config "
        "it made from yours. This is a fault in AIOSubtitles, not in your config: nothing needs doing on your side. "
        "Until it is fixed, AIOSubtitles keeps using the config it made before. AIOStreams' reason: \"{detail}\"",
        False),
    "aiostreams_errors": State(
        "Your AIOStreams answered with an error",
        "The last three checks got an error from your AIOStreams or couldn't reach it (it was busy or had a fault), so "
        "AIOSubtitles couldn't finish checking your config. It keeps using the config it made before and tries again "
        "at the next check; nothing needs doing.", False),
}
PUBLIC_NOTICE_LABEL = "AIOSubtitles: action needed"
PUBLIC_NOTICE_DONE = "AIOSubtitles: nothing needs doing any more. This entry goes away by itself."


def state_text(state, detail=None):
    """A check state's text, with the plain reason a rebuild couldn't be made in it."""
    return PUBLIC_STATES[state].text.format(detail=detail or "")


# The audio route on the public pages, shown only where the pages offer it (audio_offered): the intro's
# sentence, the privacy page's words on what is read ("{at_most}": the instance's audio limit, none without one), the
# signed-up page's note; and the note under the answer "as soon as a file is opened", shown while it is the one
# picked. Each person chooses when (PUBLIC_FIELDS["audio"]), so these name the default and leave the answers to the
# settings page. "{usenet_or}": Usenet where the instance offers it (instance_words).
PUBLIC_AUDIO = {
    "intro": ("A file with no subtitle track of its own can be synced from its sound, when you choose: by default only "
              "after you pick its \"(AIOSubtitles)\" entry. That reads about 60–350 MB of it through your debrid "
              "service{usenet_or}, and the copy is there the next time you open the file."),
    "privacy": ("; for a file with no subtitle track of its own, as you choose on your settings page (by default only "
                "after you pick its \"(AIOSubtitles)\" entry), short parts of its sound (about 60–350 MB{at_most}) "
                "through your own debrid service{usenet_or}, used only to time the subtitles and not kept;"),
    "next": ("A file with no subtitle track of its own takes one to three minutes once its sync from the sound starts "
             "(by default after you pick its \"(AIOSubtitles)\" entry; you choose on your settings page); its copy is "
             "there the next time you open it."),
    "opened_note": ("This reads about 60–350 MB of each opened file that has no subtitle track, even one you don't "
                    "watch."),
}


def audio_limit_words(settings):
    """The instance's per-file audio limit as the pages say it, in plain digits: "1 GB", "0.5 GB", "1000000 GB" (never
    "1e+06 GB"); None without a limit (SUBSYNC_AUDIO_BUDGET_GB 0)."""
    if not settings.audio_budget:
        return None
    return f"{settings.audio_budget / 1e9:f}".rstrip("0").rstrip(".") + " GB"


def intro_parts(settings):
    """The landing intro's two paragraphs, the audio route's sentence ending the first where the pages offer it."""
    first = PUBLIC_TEXTS["intro"]
    if audio_offered(settings):
        first += " " + PUBLIC_AUDIO["intro"].format(**instance_words(settings))
    return first, PUBLIC_TEXTS["intro_shared"]


# TMDB's notice (its terms ask for it, with its logo, wherever TMDB's data is used: ui.tmdb_credit), and the privacy
# page's sentence on the original-language lookups, by what the instance uses: TMDB with its own key, TMDB with each
# person's own key, Wikidata only, or no lookup at all (no sentence)
TMDB_NOTICE = "AIOSubtitles uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB."
# ("{kept}": how long TMDB's answers are kept, where the instance asks TMDB: origlang.TMDB_KEEP_S)
_LOOKUP = ("To time subtitles to the original speech, AIOSubtitles also looks up each title's original language by its "
           "id alone (IMDb, or for anime MyAnimeList or Kitsu), from its own server: at {where}. The answers are kept "
           "with no time limit{kept} and shared by everyone.")
_TMDB_KEPT = " (TMDB's up to 6 months)"
PRIVACY_LOOKUP = {
    "tmdb": _LOOKUP.format(where="TMDB, Wikidata, and for anime AniList and Kitsu", kept=_TMDB_KEPT),
    "personal": _LOOKUP.format(
        where="TMDB with your own key if you added one, Wikidata, and for anime AniList and Kitsu", kept=_TMDB_KEPT),
    "wikidata": _LOOKUP.format(where="Wikidata, and for anime AniList and Kitsu", kept=""),
    "off": "",
}
PRIVACY_TMDB_STORED = "your TMDB key if you added one, "      # in "What is stored", when each person may add one


def lookup_kind(settings):
    """Which of PRIVACY_LOOKUP's sentences the instance's settings make true."""
    if not getattr(settings, "original_language", True):
        return "off"
    if getattr(settings, "tmdb_key", ""):
        return "tmdb"
    if getattr(settings, "public", False) and getattr(settings, "tmdb_personal", False):
        return "personal"
    return "wikidata"


def tmdb_used(settings):
    """Whether the instance uses TMDB (its own key, or each person's): then TMDB's logo and notice are shown."""
    return lookup_kind(settings) in ("tmdb", "personal")


def privacy_parts(settings, sources):
    """The privacy page's parts, (heading, text), with what this instance does: the audio route's words on what is read
    and a person's answer for it among what is stored where the pages offer it; the subtitle sites it asks (`sources`:
    extra.asked_sources), Cinemeta only with Gestdown, and the lookups' sentence; a person's own TMDB key among what is
    stored when they may add one; who sees every request and how long backups and records are kept, by its
    settings; Usenet and the kept password as far as it offers them (instance_words)."""
    kind = lookup_kind(settings)
    audio, limit = audio_offered(settings), audio_limit_words(settings)
    sites = list(dict.fromkeys(SITE_NAMES[source] for source in sources))      # both OpenSubtitles ones named once
    asked = [PRIVACY_SITES.format(sites=", ".join(sites))] if sites else []
    if "gestdown" in sources:
        asked.append(PRIVACY_CINEMETA)
    if PRIVACY_LOOKUP[kind]:
        asked.append(PRIVACY_LOOKUP[kind])
    with_proxy, without = PRIVACY_FRONT
    keep = settings.user_keep_days
    words = instance_words(settings)
    fill = {**words,
            "audio": PUBLIC_AUDIO["privacy"].format(at_most=f", at most {limit}" if limit else "", **words)
            if audio else "",
            "asked": "".join(" " + sentence for sentence in asked),
            "audio_answer": PRIVACY_AUDIO_STORED if audio else "",
            "tmdb": PRIVACY_TMDB_STORED if kind == "personal" else "",
            "backups": backup_words(settings, "stored"),
            "front": with_proxy.format(proxy=settings.front_proxy) if settings.front_proxy else without,
            "backups_kept": backup_words(settings, "kept"),
            "keep": f"{keep} {plural(keep, 'day')}"}
    return tuple((head, text.format(**fill)) for head, text in PRIVACY_PARTS)


def next_text(settings):
    """The signed-up page's "what happens next", with the audio route's note where the pages offer it."""
    text = PUBLIC_TEXTS["next"]
    return text + " " + PUBLIC_AUDIO["next"] if audio_offered(settings) else text


# The sign-up page's heading in three parts, for its deliberate line breaks on desktop and phones (pages._headline).
PUBLIC_HEADLINE = ("Subtitles,", "in sync", "with the file you play.")

# What to have ready before signing up; the last line takes the attempts allowed per hour.
PUBLIC_BEFORE = (
    "The install link of your AIOStreams config and its password, both from AIOStreams' configure page.",
    "A debrid service in that config other than Real-Debrid, such as TorBox. With Real-Debrid alone, you get only the "
    "copies already synced for someone else.",
    "An AIOStreams that opens from the internet over https.",
)

# Adding AIOSubtitles by hand: the lead line, then step by step.
PUBLIC_MANUAL_LEAD = "Add AIOSubtitles to your AIOStreams config yourself:"
# The lead line instead when AIOSubtitles sent its change to the person's config but couldn't check that it went
# through, so it may have: at sign-up (and after a Refresh to stored copies, which takes the sign-up's texts),
# and on the settings page after a Refresh that rebuilt its own config
PUBLIC_UNCHECKED_LEAD = ("AIOSubtitles sent its change to your AIOStreams config but couldn't check that it went "
                         "through. If AIOSubtitles isn't among the addons there, add it yourself:")
PUBLIC_REFRESH_UNCHECKED = ("AIOSubtitles' config on your AIOStreams now matches yours, but AIOSubtitles couldn't "
                            "check that it is back in your own config. If it isn't among the addons there, add it "
                            "yourself:")
PUBLIC_MANUAL_STEPS = (
    "On AIOStreams' configure page, open Addons.",
    "Add a custom addon and paste your personal link as its manifest link.",
    "Place it before your other subtitle sources and save.",
)

# What the delete step removes ("config": not for a person with stored copies only, who has no config of its own).
PUBLIC_DELETE_LIST = {
    "data": "Everything AIOSubtitles keeps about you is deleted.",
    "config": "Its own config is removed from your AIOStreams.",
    "link": "Your personal link stops working at once.",
}
