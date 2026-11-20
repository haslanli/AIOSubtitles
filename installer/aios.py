"""The few AIOStreams API calls ./setup makes: status, read and save a config, create the sync config, sign in, and
the subtitle cache setting. Errors come back as plain sentences that say how to fix them. The client lives in
subsync/aiosconfig.py, shared with the public sign-up."""
from subsync.aiosconfig import (  # noqa: F401
    ALIAS_NO_CONFIG, ALIAS_UNKNOWN, WRONG_PASSWORD_CODES, AiosApi, AiosError, needs_sign_in)

Aios = AiosApi
