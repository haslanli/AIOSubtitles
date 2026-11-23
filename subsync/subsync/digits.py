"""Numbers written in outside text (a setting, an answer to ./setup, a request's hint or id, a header, another service's
answer), read one way everywhere: in the ASCII digits 0 to 9 only.

str.isdigit() also takes superscripts ("²"), which int() refuses, so a check with isdigit() then int() crashed on one;
and int() and float() read every script's decimal digits ("١٢", "１２") as if they were ASCII ones, so such text was
silently taken for a number. Here both are refused the way any other text that isn't a number is: the caller gets None.

Standard library only, so ./setup can import it.
"""


def whole(text):
    """The whole number `text` writes in the digits 0 to 9 alone (no sign, space, underscore or other script's digit),
    else None; also None for one longer than int() converts (its 4,300-digit limit)."""
    text = str(text)
    if not (text.isascii() and text.isdigit()):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def decimal(text):
    """The number `text` writes as float() reads it ("1.5", ".5", "2", "1e3", "inf", "nan"), in ASCII only and without
    an underscore, else None. The caller judges what it may be (finite, above 0). float() reads an underscore between
    digits as nothing, so "1_5" (1.5 mistyped) was 15: refused here as whole refuses it (S9)."""
    text = str(text)
    if not text.isascii() or '_' in text:
        return None
    try:
        return float(text)
    except ValueError:
        return None
