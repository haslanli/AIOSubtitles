"""One explanation for every setting and every fixed rule, written once and shown wherever the user meets it: with each
./setup question (and again on "?"), in docs/answers.example, in the README's settings section and above each line of
state/subsync.env. Tests fail when a setting has no explanation or a documented default differs from the code's.

Each text says what the setting does, what to choose and why, what changes either way (what you see, what is read or
downloaded, what gets faster or slower) and what it costs. Nothing in AIOSubtitles is shared between installs, so every
setting changes only the subtitles of the install it is set on; in public mode (one service for many people) how each
copy was synced (its timing, never its text) is shared between everyone who plays the same file.

No packages besides the standard library, so ./setup imports this module too.
"""
from .realdebrid import REASON

# ------------------------------------------------------------------------------------------- fixed rules

RULES = [
    ("Real-Debrid is never used",
     "AIOSubtitles never makes a single call to Real-Debrid: no search, no cache check (asking whether a file is ready), "
     f"no download, no file read, because {REASON}. With Real-Debrid beside another service, such as TorBox, subsync uses only the other one, so a file you play from "
     "Real-Debrid gets a synced copy only when that service has the same file ready. With Real-Debrid as your only "
     "debrid service, ./setup stops. subsync also checks its config before every request through it, skips files "
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
    ("Usenet is never read",
     "subsync never reads through Usenet: its config leaves out Usenet sources (Newznab indexers, NZBHydra, Easynews "
     "and similar) and Usenet services (NzbDAV, AltMount and similar). Why: every listing through an indexer spends "
     "calls of its daily limit, and opening a Usenet file usually starts a download of the whole file. A file you play "
     "from Usenet gets a synced copy only when your debrid service has the same file ready. Your indexer calls and "
     "download queue stay untouched; there is no switch for this."),
    ("Only ready debrid files are read",
     "subsync reads a file only when your debrid service already has it ready (cached), never one the service would "
     "first have to download, and never one found in your debrid account's own download list. So subsync never adds a "
     "download to your account. Files played from plain torrents or web links get no synced copy."),
    ("Only MKV and MP4 files",
     "subsync reads the subtitle tracks of MKV and MP4 files only, the formats nearly all streaming releases use. Other "
     "files (AVI, M2TS and the like) keep their original subtitles, after one small read that shows the format."),
    ("A copy is offered only after it passes the check",
     "Each synced copy is checked against the file itself before it is offered: at least 90 % of its lines must start "
     "within 200 ms (a fifth of a second) of the file's own subtitle track, spread over the whole film (95 % when its "
     "lines were snapped onto the track), or it must be clearly better than the original (SUBSYNC_BETTER). Files "
     "without a subtitle track are checked against their speech instead (the audio route). A copy that fails is never "
     "offered: you get the original subtitles, as without subsync."),
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
     "answer only under a private key in their address, so nobody who finds subsync's address can make it read files; "
     "only the synced files and a health check are open."),
    ("Public mode: your AIOStreams password is used once",
     "When you sign up or refresh, AIOSubtitles uses your AIOStreams password in that request and then forgets it, "
     "unless you tick the box that lets it keep the password: then it is kept encrypted in your record, never in a "
     "log, until you untick it. It needs it to read your config, to make a separate config for AIOSubtitles on your "
     "AIOStreams (without Real-Debrid, Usenet, AIOSubtitles itself or anything that starts work in the background, so "
     "opening a file opens only that file) and, if you leave the box checked, to add AIOSubtitles to your own config. "
     "Without that separate config, a file that failed to open could make your AIOStreams move on to another source, "
     "Real-Debrid or a Usenet download among them."),
    ("Public mode: your AIOStreams is checked about once a day",
     "While you use AIOSubtitles, it checks your AIOStreams at most once a day, in the background: whether your config "
     "still exists (a question that needs no password) and whether its own config there still opens. It repairs a "
     "changed link by itself and tells you what only you can fix, on your settings page and with one \"AIOSubtitles: "
     "action needed\" entry in your subtitle lists. Without a kept password it can't see services or addons you add "
     "or remove, so press Refresh after such a change; with one, it rebuilds its own config from yours by itself, "
     "leaving Real-Debrid and Usenet out, and never changes your config."),
    ("Public mode: Real-Debrid is left out",
     f"If your AIOStreams uses Real-Debrid, AIOSubtitles leaves it out of its own config and never reads a Real-Debrid "
     f"file, because {REASON}. It syncs only files from your other services: a file you play from Real-Debrid gets a "
     "synced copy when another of your services has the same file ready. With Real-Debrid as your only debrid service, "
     "sign-up stops, because AIOSubtitles would have nothing to read. Your own config and apps keep Real-Debrid as "
     "before, and your AIOStreams still searches it when you browse."),
    ("Public mode: a few files at once",
     "The service works on a set number of files at once for everyone (SUBSYNC_SYNCS_AT_ONCE), in the order people "
     "asked; two people playing the same file of a torrent never read it twice at once: the second gets the first "
     "one's work. A file's "
     "subtitle track is about 1 MB and is synced in seconds. Files already synced for someone are served at once. When "
     "many people play new files together, the synced copy can come after your app has picked a subtitle; the next "
     "time you open the file, it is there."),
    ("Public mode: limits per person",
     "Each person can have a few files waiting and read a set number of new files and megabytes a day (UTC), by "
     "default 3 waiting, 40 files and 500 MB, and the whole service reads at most its own daily total; files already "
     "synced for someone don't count. Past a limit, you get only copies already synced for someone until the next "
     "day, and nobody else is affected. Sign-up attempts are limited per internet address, and the service takes a "
     "set number of people. Your settings page shows today's use."),
    ("Public mode: what is kept, and for how long",
     "AIOSubtitles keeps one record per person, encrypted so that it opens only with that person's personal link: "
     "the address of their AIOStreams, the id of their config, AIOSubtitles' own config there and its password, and "
     "their languages, plus how much was read for them each day. Your AIOStreams password is never kept. For each "
     "synced subtitle only its new timing (never its text), with the names and sizes of the files it fits, is shared "
     "and kept 180 days after last use, without who played them; logs name the titles synced, never who asked. Your record is deleted when you delete it on your "
     "settings page, or 180 days after your last request."),
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
     "of your release, then the rest. Among equally likely ones, your config's addons come first, then OpenSubtitles, "
     "TheSubtitleDB, SubDL, SubSource, Gestdown and Wyzie. With no subtitle addon in your config, no subtitle has a "
     "place of its own as your first."),
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
# answers-file key -> (example value for docs/answers.example, explanation), in the order ./setup asks them

QUESTIONS = {
    "AIOS_CONTAINER": (
        "",
        "Asked only when more than one AIOStreams runs in Docker on this machine: the one your apps use. subsync joins "
        "that container's Docker network and finds files through it, so the wrong one means it finds none of the files "
        "you play. Pick the one whose address your apps use."),
    "AIOS_URL": (
        "http://127.0.0.1:3000",
        "Asked only when ./setup can't find AIOStreams running in Docker on this machine: the address of AIOStreams' "
        "configure page, without /stremio/..., like http://127.0.0.1:3000 or https://aio.example.com. subsync talks to "
        "AIOStreams at this address, so it must work from this machine."),
    "AIOS_CONFIG_ID": (
        "",
        "The ID of the AIOStreams config your apps use: a long code like 1a2b3c4d-…, shown on AIOStreams' configure page "
        "when you save or load the config, and part of its install link (…/stremio/<ID>/…). You can paste the whole "
        "install link: ./setup takes the ID out of it and checks that the config exists. subsync is added to this "
        "config, and subsync's own config is made from it."),
    "AIOS_CONFIG_PASSWORD": (
        "",
        "The password you chose when you saved that config. ./setup uses it to read the config and save the change, and "
        "doesn't store it; subsync gets its own config with its own random password. While setting up, Enter with "
        "nothing typed goes back to give a different ID; ./setup --restore and --uninstall need it to change the config. "
        "If you put it in an answers file, keep that file private."),
    "LANGUAGES": (
        "English",
        "The subtitle languages to sync, by name or code, separated by commas: English, Spanish, ru; up to four, "
        "default English. For each language and each file you play, subsync downloads that language's subtitles from "
        "your subtitle sources and the ones it searches itself, and checks them against the file. Each extra language "
        "adds those downloads and a few seconds of work per file; the video file is still read only once."),
    "AUDIO": (
        "no",
        "Also sync files that have no subtitle track of their own, by listening to their speech. Recommended: no, "
        "unless you often watch such files; most files have a track. Off: those files keep their original subtitles, "
        "and subsync reads about 1 MB of each file. On: after you pick such a file's \"<Language> (Subsync)\" entry, "
        "subsync reads short audio parts of it, up to the limit you choose next (1 GB recommended; counted in the "
        "daily limit and in your debrid service's traffic), and the copy is there the next time you open the file; "
        "files with too high a bitrate for that limit keep their originals."),
    "AUDIO_BUDGET": (
        "1",
        "Asked only with the audio route on: the most it reads of one file, through your debrid service: 0.5, 1, 2 "
        "or 4 GB. Recommended: 1: it covers nearly every file with no subtitle track (web releases, MP4s, "
        "re-encodes); remuxes usually carry image subtitles and need no audio. Higher: very high-quality files such "
        "as 4K remuxes get synced too, but each can cost that much data and take longer than the 14 seconds an app "
        "waits, so the copy comes the next time you open the file. Lower: less data on a metered connection; more "
        "high-bitrate files keep their originals. Smaller files are read only as far as needed. A change makes "
        "subsync judge again the files it kept as originals."),
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
        "AIOStreams, like https://aio.example.com/subsync, which needs one rule in your web server (./setup shows it); "
        "on a home network, this machine's own port works, like http://192.168.1.5:8790. It must work from every device "
        "you watch on, outside your home too if you watch there; otherwise entries show but fail to load. Only synced "
        "files and a health check are open at that address."),
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
        "Save the changes ./setup just showed: subsync added to your config (after a backup copy) and subsync's own "
        "config created or updated. Default yes. No: nothing is changed; run ./setup again any time. ./setup --restore "
        "and --uninstall ask the same before they change anything, and there the default is no."),
    "TEST_READ": (
        "no",
        "A small test read at the end: subsync reads the start and the index of one ready file (usually under 1 MB), as "
        "it does for every file you play. Yes: you see now that reading works and whether that file has its own "
        "subtitle track; your debrid service opens the file, so it may show in your debrid account's list. No "
        "(default): the first file you play is the first read."),
    "REMOVE_OPENSUBTITLES": (
        "yes",
        "Asked by ./setup --uninstall when ./setup had added OpenSubtitles to your config. Yes takes it out again, "
        "leaving your config as it was before subsync; no keeps it, so your apps keep getting OpenSubtitles' "
        "subtitles."),
}

# ------------------------------------------------------------------------------------------- subsync's settings
# environment name -> (default as written in the environment, None when required; explanation), grouped for the README

_THRESHOLD = "Higher or lower than recommended trades how many copies are offered against how closely they fit."

SETUP_GROUP = "Written by ./setup"   # ./setup owns these lines of state/subsync.env; it keeps every other line

GROUPS = [
    (SETUP_GROUP, {
        "SUBSYNC_SOURCE_MANIFEST": (
            None,
            "The link of subsync's own AIOStreams config, which subsync finds files and subtitles through. ./setup makes "
            "that config from yours, without Usenet, Real-Debrid, subsync itself or anything that works in the "
            "background, and writes this. Required: subsync doesn't start without it. Keep it private: anyone with it "
            "can list through that config."),
        "SUBSYNC_SOURCE_PASSWORD": (
            None,
            "The password of subsync's own config: random, made by ./setup, never yours. Before every request through "
            "that config, subsync reads it with this password and checks that it holds no way to Real-Debrid; when the "
            "check fails or the password is wrong, nothing is listed and the log says why. Required: subsync doesn't "
            "start without it. Keep it private."),
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
            "make it read files; synced files and /health stay open for your apps. Made by ./setup; keep it private. "
            "Empty: no key and every route open, safe only where nobody else can reach subsync."),
        "SUBSYNC_LANGUAGES": (
            "eng",
            "The languages to sync, as codes separated by commas, like eng,rus: ./setup writes your answer to its "
            "languages question. Each language is a separate sync for every file you play, with its own subtitle "
            "downloads and a few seconds of work; the video file is read only once."),
        "SUBSYNC_AUDIO_ROUTE": (
            "off",
            "Your answer to ./setup's audio question, on or off: whether files with no subtitle track of their own are "
            "synced from their speech, reading up to SUBSYNC_AUDIO_BUDGET_GB of audio each after you pick their "
            "\"(Subsync)\" entry. Recommended: off unless you often watch such files. Off: they keep their original "
            "subtitles and subsync reads about 1 MB per file."),
        "SUBSYNC_AUDIO_BUDGET_GB": (
            "1",
            "With the audio route on, the most audio subsync reads of one file, in GB: your answer to ./setup's audio "
            "limit question (0.5, 1, 2 or 4), written only while the audio route is on. Within it, high-bitrate files "
            "get shorter audio parts, or none when they would be too short to judge, and then keep their originals; a "
            "smaller file is read only as far as needed. Recommended: 1. Higher: very high-quality files such as 4K "
            "remuxes get copies too, at up to that much data each. Lower: less data, more such files keep their "
            "originals. Another number set here by hand works too; 0 removes the limit, which is not recommended."),
        "SUBSYNC_URL_REWRITE": (
            "",
            "Rewrites the start of stream links, like https://aio.example.com=http://aiostreams:3000, so subsync reads "
            "files from AIOStreams over Docker on this machine instead of out through your web server and back. "
            "./setup sets it when both run here and leaves it empty when they don't. A wrong rule makes every read fail, "
            "and files keep their original subtitles."),
        "SUBSYNC_SUBDL_KEY": ("", QUESTIONS["SUBDL_KEY"][1]),
        "SUBSYNC_WYZIE_KEY": ("", QUESTIONS["WYZIE_KEY"][1]),
        "SUBSYNC_PORT": (
            "8790",
            "The port subsync listens on: inside its container, or on the machine itself when AIOStreams uses host "
            "networking. Change it only when 8790 is taken, by giving ./setup's address question another port; ./setup "
            "then sets it."),
    }),
    ("Limits and safety", {
        "SUBSYNC_DAILY_BUDGET_GB": (
            "50",
            "The most subsync reads from files in one day (UTC), in GB received. When it's spent, reading stops until "
            "the next day and new files keep their original subtitles. 50 is far above normal use (about 1 MB per file; "
            "up to 1 GB per file with the audio route), so it only catches something going wrong. Lower it if your "
            "debrid service or server has a tight traffic limit."),
        "SUBSYNC_INDEX_CAP_MB": (
            "16",
            "The most subsync reads of one file to find its subtitle tracks (the start, the index, and 64 KB at each "
            "end), in MB. Most files need about 1 MB; files with very many tracks need more, and one needing more than "
            "this keeps its original subtitles. Recommended: 16. Higher: a few more such files get copies, each costing "
            "that much more data."),
        "SUBSYNC_FILE_CAP_GB": (
            "6",
            "The most subsync reads of one file in all when the audio route works on it, in GB: an outer safety limit, "
            "since the audio limit (SUBSYNC_AUDIO_BUDGET_GB, at most 4 GB from ./setup) normally stops first. "
            "Recommended: 6. 0 removes it, which is not recommended."),
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
            "How long one read of a file may wait for an answer, in seconds, before it counts as failed; a failed file "
            "is tried again 1, 6 and 24 hours later. Recommended: 60. Shorter: slow services fail more often."),
        "SUBSYNC_SIZE_SLACK_MB": (
            "5.5",
            "How far a file's listed size may be from its real size and still be taken for the same file, in MB; "
            "listings round sizes to 0.01 GiB, up to 5.4 MB off. Recommended: 5.5. Lower: rounded listings stop "
            "matching and those files get no copy. Higher: another file of the same name could be taken, though its "
            "own hash still catches that."),
        "SUBSYNC_STORE_CAP_MB": (
            "2048",
            "Disk space for synced copies, in MB; past it, the copies served longest ago are deleted and made again "
            "when needed, which costs a new read. 2048 holds about 20,000 copies. Lower it on a small disk."),
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
            "that is already an addon in your config isn't asked twice."),
        "SUBSYNC_SOURCE_TIMEOUT_S": (
            "5",
            "How long AIOSubtitles waits for each subtitle site it searches itself, in seconds. A site that answers later "
            "is left out for this play and its answer is kept for the next one, so a slow site never makes your first "
            "subtitle late. Recommended: 5. Longer: slow sites count more often, but your first subtitle can come later. "
            "A site that fails three times in ten minutes is paused for ten minutes, then tried again."),
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
        "SUBSYNC_SNAP": (
            "on",
            "On: after re-timing, each line moves to start exactly where the file's own matching line starts, wherever "
            "that match is clear. Recommended: on: lines appear right on cue. Off: lines keep their author's timing "
            "inside the re-timed blocks."),
        "SUBSYNC_SNAP_WITHIN": (
            "0.95",
            "The share of lines that must start within 200 ms of the file's track for a copy whose lines were snapped "
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
            "With the audio route on: on, subsync reads a file's audio only after you pick its \"(Subsync)\" entry, "
            "which shows you are playing it; off, on any subtitle request, including the ones AIOMetadata sends for a "
            "version you never play. Recommended: on: no audio is read for files you only browse. Off: copies come "
            "sooner, at up to 1 GB read for every file merely opened."),
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
            "Audio route, \"clearly closer\": at least this share of lines must start within 200 ms of the speech "
            "(0.55 = 55 %), lower than for a subtitle track because speech detection is less exact. Recommended: 0.55. "
            + _THRESHOLD),
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
        "SUBSYNC_PUBLIC": (
            "off",
            "Public mode: one AIOSubtitles service for many people, like aiosubtitles.com. Each person signs up on its "
            "web page with their AIOStreams link and password and gets a personal addon link; subsync then works "
            "through a separate sync config it makes on their AIOStreams. Leave it off for your own install: ./setup "
            "doesn't use it. On, the service needs SUBSYNC_SECRET_KEY and SUBSYNC_PUBLIC_BASE, keeps the audio route "
            "off, and shares how each copy was synced (its timing, never its text) between everyone who plays the same "
            "file, so the more people use it, the more files are ready when someone presses play. It holds other people's links, so it needs a "
            "server you keep up to date and backed up."),
        "SUBSYNC_SECRET_KEY": (
            "",
            "Public mode only: the service's secret, at least 32 random bytes written as base64url, for example what "
            "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\" prints. It encrypts each person's record "
            "together with their personal link, so neither the database nor this secret alone opens a record. "
            "Required in public mode; never logged. Keep it in a private file and in your backups: if it is lost or "
            "changed, everyone has to sign up again."),
        "SUBSYNC_PUBLIC_INVITE": (
            "",
            "Public mode only: when set, the sign-up page asks for this code, so only people you gave it to can sign "
            "up, for example while you and your testers try the service. Empty lets anyone sign up. It doesn't affect "
            "people who already signed up. Recommended while testing; remove it to open the service."),
        "SUBSYNC_MAX_USERS": (
            "200",
            "Public mode only: how many people can be signed up at once; past it the sign-up page says the service is "
            "full. Each person costs a little disk and, while they play, a worker, so the limit keeps the server "
            "within what it can carry. Raise it only after watching memory, processor use and the daily read total "
            "for a while."),
        "SUBSYNC_SIGNUPS_PER_HOUR": (
            "5",
            "Public mode only: sign-up attempts allowed per internet address per hour, counted before anything is "
            "fetched. Each attempt makes the service contact that person's AIOStreams several times, so this stops one "
            "address from flooding the service or someone else's AIOStreams. 5 is plenty for someone who mistypes a "
            "password; lower is stricter, higher helps many people behind one shared address."),
        "SUBSYNC_USER_FILES_PER_DAY": (
            "40",
            "Public mode only: new files read per person per day (UTC). Files already in the shared store don't count, "
            "and neither does serving a copy. Past it, that person gets only copies the store already has until the "
            "next day; nobody else is affected. 40 is several days of watching in one; it keeps one person, or a "
            "leaked link, from using up the service's daily reads."),
        "SUBSYNC_USER_DAILY_MB": (
            "500",
            "Public mode only: megabytes read per person per day (UTC) through their own sync config. Reading a "
            "file's subtitle track takes about 1 MB, so this is room for hundreds of files; it stops someone whose "
            "files have unusual layouts from using most of the service's daily total (SUBSYNC_DAILY_BUDGET_GB). Past "
            "it, that person gets only copies already in the store until the next day."),
        "SUBSYNC_USER_QUEUE": (
            "3",
            "Public mode only: files one person may have waiting at once. A request past it is answered with what the "
            "shared store already has and starts nothing. Everyone's files wait for the same few places "
            "(SUBSYNC_SYNCS_AT_ONCE), so a longer queue for one person delays everyone else; 3 covers someone opening "
            "a few episodes in a row."),
        "SUBSYNC_ACTIVE_WORKERS": (
            "30",
            "Public mode only: how many people can have a worker (the part that finds and syncs their files) running "
            "at once. A worker with nothing to do for SUBSYNC_WORKER_IDLE_MIN stops. Past the limit, a person's "
            "requests get only copies already in the shared store until a worker frees up. Each worker costs a few "
            "megabytes of memory; keep the limit within what the server's memory allows."),
        "SUBSYNC_WORKER_IDLE_MIN": (
            "30",
            "Public mode only: minutes without a request before a person's worker stops and their opened record "
            "leaves memory. Shorter frees memory sooner and keeps opened records in memory for less time; longer saves "
            "reopening the record when someone pauses between episodes. Reopening is quick, so 30 is a fair middle."),
        "SUBSYNC_USER_KEEP_DAYS": (
            "180",
            "Public mode only: days without a single request before a person's record is deleted. Their sync config "
            "can't be removed from their AIOStreams then, because opening the record needs their personal link; the "
            "privacy page says so. Shorter keeps less data about people who left; longer spares people who come back "
            "after a long break from signing up again."),
        "SUBSYNC_TRUSTED_PROXY": (
            "127.0.0.1",
            "Public mode only: the addresses of the web server in front of subsync, comma-separated. Only requests "
            "from these may say, in their X-Forwarded-For header, which address they came from, and the sign-up limit "
            "per address counts that one. Set it to your web server's address as subsync sees it, or to gateway when "
            "the web server reaches subsync through a port Docker publishes: subsync then trusts its Docker network's "
            "gateway, found at start, with no fixed network to set up. A wrong value makes every visitor count as one "
            "address, or lets visitors choose their own."),
        "SUBSYNC_MAX_CONNECTIONS": (
            "64",
            "Public mode only: requests handled at once. Each one takes a thread and some memory, so past it the "
            "service answers \"busy, try again\" (HTTP 503) at once instead of running out of memory. Apps ask for "
            "subtitles again on their own. Raise it together with the memory the service may use."),
        "SUBSYNC_RECHECK_HOURS": (
            "24",
            "Public mode only: how often, at most, each person's AIOStreams is checked again while they use the service, "
            "in hours; 0 turns the check off. A check asks their AIOStreams two things (does their config still exist, "
            "does AIOSubtitles' own config there still open), repairs a changed link, and tells the person on their "
            "settings page and in their subtitle lists what only they can fix; for people who let AIOSubtitles keep "
            "their password it also rebuilds AIOSubtitles' config from theirs. Shorter notices changes sooner, at "
            "more calls to every AIOStreams; people who don't use the service aren't checked."),
        "SUBSYNC_SYNCS_AT_ONCE": (
            "1",
            "Public mode only: how many files the service works on at once for everyone, or auto for as many as the "
            "processors it may use. More lets people who play new files at the same time wait less, at more processor "
            "use and more requests to each AIOStreams at once; one public AIOStreams shared by many people limits how "
            "many requests one address may make, so 2 suits a server with 2 processors. A torrent's file is never "
            "read twice at once: whoever plays it second waits for the first one's work and reads nothing."),
    }),
]

SETTINGS = {name: entry for _, group in GROUPS for name, entry in group.items()}
WRITTEN_BY_SETUP = tuple(dict(GROUPS)[SETUP_GROUP])

# ------------------------------------------------------------------------------------------- public mode's page texts

PUBLIC_REAL_DEBRID_WARNING = (
    f"Real-Debrid is left out: AIOSubtitles never uses it, because {REASON}. It syncs only files from your other "
    "services, so a file you play from Real-Debrid gets a synced copy only when another of your services has the "
    "same file ready.")

PUBLIC_PASSWORD_TEXT = (
    "Your AIOStreams password, used now for two things: to add AIOSubtitles to your AIOStreams config (if you leave the "
    "box below checked), and to make a separate AIOStreams config for AIOSubtitles without Real-Debrid, Usenet or "
    "automatic fallback to other sources, so opening a file opens only that file. It is not stored unless you tick "
    "\"Keep my password\" below. You'll be asked again only if you refresh.")

CONTACT = "https://github.com/haslanli/AIOSubtitles/issues"

# one explanation for each field and button of the public pages
PUBLIC_FIELDS = {
    "link": (
        "The link of your AIOStreams config, the one your apps use: on AIOStreams' configure page, copy the install "
        "link (it ends in /manifest.json). AIOSubtitles reads your config through it once, now, and makes a separate "
        "config next to it on your AIOStreams. Your AIOStreams must be reachable from the internet over https. Keep "
        "the link private: anyone with it can use your AIOStreams config."),
    "password": PUBLIC_PASSWORD_TEXT,
    "languages": (
        "The languages you want synced subtitles in, up to four, like English, Spanish. Each one is a separate sync "
        "for every file you play, so fewer is faster. The first one gets up to three synced choices, the others one "
        "each. You can change them later; a change applies from the next file you play."),
    "add_addon": (
        "Adds AIOSubtitles to your AIOStreams config, before your other subtitle sources, so your apps show its "
        "synced subtitles first (many apps show only a few per language). Recommended. Unchecked, nothing in your own "
        "config changes and you add the link yourself as a custom addon."),
    "clean_up": (
        "Takes out of your AIOStreams config the subtitle addons AIOSubtitles now searches itself: OpenSubtitles, "
        "OpenSubtitles V3+, SubDL and SubSource. You then get their subtitles once, through AIOSubtitles: synced where "
        "one fits, the original otherwise, instead of the same ones twice. Your other subtitle addons stay. The cost: "
        "while AIOSubtitles is down, these subtitles are missing too, until it is back. Unchecked, your config keeps "
        "them and nothing is removed. It works only together with adding AIOSubtitles; to undo it, add the addons "
        "again on AIOStreams' configure page."),
    "invite": "The code you were given. AIOSubtitles is open only to invited people for now.",
    "refresh": (
        "Your AIOStreams password, used now to read your config again and rebuild AIOSubtitles' own config from it, for "
        "example after you added or removed a service, to make that config again if it is gone, and to put "
        "AIOSubtitles back in your config if it is missing. It is not stored unless \"Keep my password\" is ticked."),
    "new_link": (
        "Optional: the link of another AIOStreams config, if you moved to a new one. AIOSubtitles reads that config "
        "with the password above, follows it from now on, and moves its own config there if it is on another "
        "AIOStreams; your personal link stays the same. Empty: AIOSubtitles keeps following the config you signed up "
        "with."),
    "keep_password": (
        "Off by default. Ticked: AIOSubtitles keeps your AIOStreams password, encrypted so that it opens only with your "
        "personal link, and about once a day while you use it rebuilds its own config from yours, so services and "
        "addons you add or remove are followed without Refresh; Real-Debrid and Usenet stay out, and your own config "
        "is never changed. The cost: that password can change your whole AIOStreams config, all your keys included, "
        "so if AIOSubtitles' server were broken into while you use it, it could be read. Unticked: it is used once and "
        "forgotten. Untick and refresh, or press \"Stop keeping my password\", to delete it at once; the encrypted "
        "backups keep it up to 30 days more."),
    "forget_password": (
        "Deletes the AIOStreams password AIOSubtitles kept, at once (the encrypted backups keep it up to 30 days more); "
        "nothing else changes. AIOSubtitles then stops following changes to your config by itself, and you press "
        "Refresh after you change it."),
    "delete": (
        "Deletes everything AIOSubtitles keeps about you and removes its own config from your AIOStreams; your "
        "personal link stops working at once. Optional: with your AIOStreams password it also takes itself out of "
        "your AIOStreams config; without it, remove the AIOSubtitles addon there yourself. The password is used once "
        "and not stored. How subtitles were synced stays in the shared store: it belongs to files, not to you."),
}

# the other texts of the public pages
PUBLIC_TEXTS = {
    "intro": (
        "AIOSubtitles gives you subtitles that are in sync with the file you play. It works through your AIOStreams: "
        "for each file you play, it reads that file's own subtitle track (about 1 MB) through a separate config it "
        "makes on your AIOStreams, and offers subtitles from your addons and the sites it searches itself, re-timed to "
        "fit. A file synced once is ready for everyone who plays it: its timing is kept and applied to the subtitle, "
        "downloaded fresh for each person."),
    "signed_up": (
        "Keep this link: it is the only key to your settings, and nobody, AIOSubtitles included, can recover it. If you "
        "lose it, sign up again. Keep it private too: anyone with it can change your settings or delete your data."),
    "manual": (
        "Add AIOSubtitles to your AIOStreams config yourself: on AIOStreams' configure page, open Addons, add a "
        "custom addon, paste the link above as its manifest link, place it before your other subtitle sources and "
        "save."),
    "not_added": (
        "AIOSubtitles couldn't add itself to your AIOStreams config, so nothing there was changed."),
    "deleted": "Your data is deleted, and your personal link no longer works.",
    "sync_left": (
        "AIOSubtitles couldn't remove its own config from your AIOStreams. It stays there unused and holds the same "
        "debrid keys as your config; whoever runs your AIOStreams can delete it."),
    "addon_left": (
        "Remove the AIOSubtitles addon from your AIOStreams config yourself (Addons, then delete it and save); its "
        "link no longer works."),
}


# The privacy page in four parts, each under its heading; the lead-in words ("What is read:") are part of the text, so
# PUBLIC_TEXTS["privacy"] is the same text as one paragraph.
PRIVACY_PARTS = (
    ("What is read", "What is read:",
     "your AIOStreams config, once at sign-up and when you refresh; then, for each file you play, its subtitle track "
     "(about 1 MB) and the subtitles your AIOStreams lists, through AIOSubtitles' own config on your AIOStreams. "
     "AIOSubtitles also searches subtitle sites itself (OpenSubtitles, TheSubtitleDB, Gestdown, SubDL, SubSource): they "
     "get the title's IMDb id, season and episode, the languages, and the file's name, size and hashes, from "
     "AIOSubtitles' server, never your internet address."),
    ("What is stored", "What is stored:",
     "one record, encrypted so that it opens only with your personal link (your AIOStreams address, AIOSubtitles' own "
     "config there and its password, your config's id, your languages, and when your AIOStreams was last checked), and "
     "how much was read for you each day. AIOSubtitles' own config holds your debrid keys, as your own config does, and "
     "the service can open it while your requests come in. Your AIOStreams password is stored only if you tick \"Keep "
     "my password\", in the same encrypted record, until you untick it (and up to 30 days more in the encrypted "
     "backups). For each synced subtitle only its new timing "
     "(never its text), with the names and sizes of the files it fits, is shared and kept 180 days after last use, "
     "without who played them. Logs name the titles "
     "synced, never who asked."),
    ("Who else sees your requests", "",
     "AIOSubtitles runs behind Cloudflare, which passes every request on and so sees your internet address and the "
     "links you open, your personal link included. AIOSubtitles itself keeps no log of requests. Backups of the stored "
     "data are kept for up to 30 days."),
    ("How long it is kept", "How long:",
     "until you delete your data on your settings page, or 180 days after your last request; then AIOSubtitles' own "
     "config can't be removed from your AIOStreams, because opening your record needs your link."),
)
# What the daily check (recheck.py) found, as the settings page and the "action needed" entry say it: state ->
# (title, text, whether only the person can fix it). "{detail}" is the plain reason a rebuild couldn't be made.
PUBLIC_STATES = {
    "sync_gone": (
        "AIOSubtitles' config on your AIOStreams is gone",
        "AIOSubtitles can no longer open the separate config it made on your AIOStreams, so it can't sync new files for "
        "you. Press Refresh with your AIOStreams password: it makes that config again.", True),
    "main_gone": (
        "The AIOStreams config you signed up with is gone",
        "Your AIOStreams no longer has the config you signed up with. If you moved to a new config, press Refresh with "
        "its link and password: AIOSubtitles then follows it, and your personal link stays the same.", True),
    "password_refused": (
        "Your kept password no longer works",
        "AIOSubtitles couldn't open your AIOStreams config with the password it kept, so it deleted that password and "
        "stopped following your config by itself. If you changed your password, press Refresh with the new one, and "
        "tick \"Keep my password\" to let it keep the new one.", True),
    "cannot_rebuild": (
        "Your AIOStreams config can't be followed as it is",
        "AIOSubtitles tried to follow a change in your AIOStreams config and couldn't: {detail} Until that changes, it "
        "keeps using the config it made before.", True),
    "reads_refused": (
        "Your debrid service refused AIOSubtitles' reads",
        "Your debrid service refused AIOSubtitles' last reads, so it pauses and tries again later. If you changed your "
        "debrid key in AIOStreams, press Refresh so AIOSubtitles' config gets the new one.", True),
    "unreachable": (
        "AIOSubtitles couldn't reach your AIOStreams",
        "The last three checks couldn't reach your AIOStreams from the internet. While it can't be reached, AIOSubtitles "
        "can't sync new files for you; nothing needs doing once it is back.", False),
}
PUBLIC_NOTICE_LABEL = "AIOSubtitles: action needed"
PUBLIC_NOTICE_DONE = "AIOSubtitles: nothing needs doing any more. This entry goes away by itself."

PUBLIC_TEXTS["privacy"] = " ".join(f"{lead} {text}".strip() for _, lead, text in PRIVACY_PARTS)

# The sign-up page's heading, with the words the hand-drawn mark goes under.
PUBLIC_HEADLINE = ("Subtitles,", "in sync", "with the file you play.")

# What to have ready before signing up; the last line takes the attempts allowed per hour.
PUBLIC_BEFORE = (
    "The install link of your AIOStreams config and its password, both from AIOStreams' configure page.",
    "A debrid service in that config other than Real-Debrid, such as TorBox.",
    "An AIOStreams that opens from the internet over https.",
)

# Adding AIOSubtitles by hand, step by step (the words of PUBLIC_TEXTS["manual"]).
PUBLIC_MANUAL_STEPS = (
    "On AIOStreams' configure page, open Addons.",
    "Add a custom addon and paste your personal link as its manifest link.",
    "Place it before your other subtitle sources and save.",
)

# What the delete step removes.
PUBLIC_DELETE_LIST = (
    "Everything AIOSubtitles keeps about you is deleted.",
    "Its own config is removed from your AIOStreams.",
    "Your personal link stops working at once.",
)

PUBLIC_TEXTS.update({
    "tries": "Each try counts: {n} sign-up attempts per address in an hour.",
    "kept_note": "For your safety, the link, password and invite code aren't kept after an error: enter them again.",
    "password_once": "Your password is used for this sign-up only and is not stored, unless you tick \"Keep my password\".",
    "copied": ("AIOSubtitles' config on your AIOStreams was copied from yours {when}. After you add or remove a service or "
               "addon in AIOStreams, press Refresh."),
    "copied_auto": ("AIOSubtitles' config on your AIOStreams was copied from yours {when}. AIOSubtitles keeps your "
                    "password and follows changes to your config by itself, about once a day while you use it."),
    "forgot": ("Your kept password is deleted. AIOSubtitles no longer follows your config by itself: press Refresh after "
               "you change it."),
    "notice_where": "Open your AIOSubtitles settings page: your personal link with /configure at the end.",
    "made_config": ("Made AIOSubtitles' own config on your AIOStreams, without Real-Debrid, Usenet or automatic "
                    "fallback to other sources."),
    "languages_chosen": "Languages: {names}.",
    "next": ("Play something in your app. For each file, AIOSubtitles offers synced copies of your subtitles in your "
             "app's subtitle list. A file nobody has played before takes a few seconds; if your app has already picked "
             "a subtitle by then, the synced copy is there the next time you open the file."),
    "settings_link": "It is your personal link with /configure at the end. Bookmark it to come back.",
    "settings_lead": ("This page opens with your personal link. Keep the link private: anyone with it can change these "
                      "settings or delete your data."),
    "reset": "Resets at 00:00 UTC, {when}. Files that already have a synced copy don't count.",
    "limit_reached": "Until 00:00 UTC you get only copies already synced for someone; nobody else is affected.",
    "languages_saved": "Languages saved: {names}. They apply from the next file you play.",
    "refreshed": "Refreshed: AIOSubtitles' config on your AIOStreams now matches yours.",
    "refresh_manual": ("AIOSubtitles' config on your AIOStreams now matches yours, but AIOSubtitles couldn't put itself "
                       "back in your own config."),
    "undone": "This can't be undone.",
    "not_found": ("If this was your personal link, its data may have been deleted, or the link was cut short: copy the "
                  "whole link again."),
    "strip": ("An illustration: your subtitles slide along the file's own subtitle track until their lines land on "
              "it."),
})

