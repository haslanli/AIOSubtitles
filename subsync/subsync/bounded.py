"""Maps kept to a number of entries, the oldest dropped first (a dict keeps insertion order): the worker's in-memory maps,
the texts rebuilt for a request (addon.App.text_of), and the built-in sources' answers and ids (extra.ExtraSources).

Standard library only.
"""


def put_bounded(d, key, value, limit):
    """d[key] = value as the newest entry of a map kept to `limit` entries, the oldest dropped first: a key set again
    moves to the end (a dict keeps a key set again in its first place, so the entry just set would go first)."""
    d.pop(key, None)
    d[key] = value
    trim_oldest(d, limit)


def trim_oldest(d, limit):
    """Drops a map's oldest entries (a dict keeps insertion order) past `limit`."""
    while len(d) > limit:
        d.pop(next(iter(d)))
