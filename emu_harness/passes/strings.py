"""Text in ROM bytes (row L of MIGRATION.md): find it, judge it, name it.

Strings are the purest oracle a walk has -- the bytes name themselves -- so they are found FIRST,
before any routine is named, and a routine that references `msg_recruit` is the recruit command.
Console-neutral: a caller hands over bytes and the address the first byte is seen at.

  nul_runs        pure_strings' scan: printable runs, each ended by a NUL or a non-printable
  printable_runs  dump_strings' scan: every printable run of a minimum length
  is_text         pure_strings' strict English filter (tile/CHR data misread as ASCII fails it)
  starts_clean    ref_strings' test for a REFERENCED string: control codes (ESC $1B, newline, field
  + is_string     separator, tab) may lead, binary bytes may not, and a real word run must follow
  slug_*          the three name stems in use (registries keep their own; see each)

KOEI text is plain ASCII ended by $00, with $0A for a newline and $1B escape sequences.
"""
import re
from collections import Counter

VOWELS = set(b"aeiouAEIOU")
CTRL = {0x1B, 0x0A, 0x01, 0x09}          # ESC-seq, newline, field-sep, tab
WORD = re.compile(r"[A-Za-z][A-Za-z0-9 ,.'%!?:;()/&-]{3,}")


def nul_runs(data):
    """[(offset, raw)] for every run of printable ASCII ended by a NUL (pure_strings.scan_bank)."""
    out, i = [], 0
    while i < len(data):
        if data[i] == 0:
            i += 1; continue
        j = i
        while j < len(data) and data[j] != 0 and 32 <= data[j] < 127:
            j += 1
        terminated = j >= len(data) or data[j] == 0
        if terminated:
            out.append((i, bytes(data[i:j])))
        i = j + 1
    return out


def printable_runs(data, minlen=3):
    """[(offset, raw)] for every printable run of at least `minlen` (dump_strings.strings)."""
    out, s = [], None
    for i, c in enumerate(data):
        printable = 32 <= c < 127
        if printable and s is None:
            s = i
        elif not printable and s is not None:
            if i - s >= minlen:
                out.append((s, bytes(data[s:i])))
            s = None
    return out


def nul_slots(data):
    """[(index, offset, raw)]: the data as NUL-delimited fields, every non-empty slot numbered
    (dump_strings.table: a fixed-slot name table)."""
    out, idx, cur, start = [], 0, bytearray(), 0
    for i, c in enumerate(data):
        if c == 0:
            if cur:
                out.append((idx, start, bytes(cur))); idx += 1
            cur = bytearray(); start = i + 1
        elif 32 <= c < 127:
            if not cur:
                start = i
            cur.append(c)
        else:
            if cur:
                out.append((idx, start, bytes(cur))); idx += 1
            cur = bytearray()
    return out


def is_text(raw):
    """Strict English/text filter -- separates real strings from tile/CHR data misread as ASCII."""
    if len(raw) < 4:
        return False
    if sum(1 for x in raw if 32 <= x < 127) < 0.98 * len(raw):
        return False
    letters = [x for x in raw if 65 <= x < 91 or 97 <= x < 123]
    if len(letters) < 0.45 * len(raw):
        return False
    if max(Counter(raw).values()) > 0.5 * len(raw):        # MMMMMM, JJJJ -> tile/attribute data
        return False
    if not re.search(rb'[A-Za-z]{3}', raw):                 # a real 3-letter word run
        return False
    if b' ' not in raw:                                      # no space: must still look word-like
        if not any(x in VOWELS for x in raw):
            return False
        vr = sum(1 for x in raw if x in VOWELS) / len(letters)
        if not (0.12 <= vr <= 0.75):
            return False
    return True


def starts_clean(raw):
    """True if a real string BEGINS at raw[0] (raw = the bytes up to, not including, the NUL): the
    bytes before the first letter are few and control/printable only (not a data table whose text
    belongs to a later record), and the whole run is mostly printable-or-control."""
    if len(raw) < 4 or len(raw) > 120:
        return False
    i = 0
    while i < len(raw) and not (65 <= raw[i] < 91 or 97 <= raw[i] < 123):
        if not (raw[i] in CTRL or 32 <= raw[i] < 127):
            return False                                     # binary byte -> data table
        i += 1
        if i > 6:
            return False                                     # letters too deep -> later record
    good = sum(1 for x in raw if 32 <= x < 127 or x in CTRL)
    return good >= 0.92 * len(raw)


def longest_word(raw):
    vis = "".join(chr(x) for x in raw if 32 <= x < 127)
    runs = WORD.findall(vis)
    return max(runs, key=len).strip() if runs else ""


def is_string(longest):
    if len(longest) < 4:
        return False
    if sum(1 for c in longest if c.isalpha()) < 3:
        return False
    return any(c in "aeiouAEIOU" for c in longest)


def referenced_text(raw):
    """ref_strings' verdict for bytes a pointer leads to: a string that may lead with control codes."""
    return starts_clean(raw) and is_string(longest_word(raw))


# ---- name stems -----------------------------------------------------------------------------
def slug_words(txt):
    """pure_strings / ref_strings: the first five words, format specifiers dropped (40 chars)."""
    t = re.sub(r'%[0-9]*[dsxc]', '', txt)
    t = re.sub(r'[^A-Za-z0-9 ]', ' ', t).strip().lower()
    words = [w for w in t.split() if w][:5]
    return ("_".join(words) or "blank")[:40]


def slug_anchor(text, maxlen=30):
    """rom-xref: anchored on the first letter (so cursor / escape prefixes of one message converge),
    and a letterless format string named by its specifiers (fmt_...)."""
    s = re.sub(r'\[[0-9a-f]{2}\]', '', text)
    s = s.replace('\\n', ' ').replace('\\r', ' ')
    m = re.search(r'[A-Za-z].*', s)
    core = m.group(0) if m else s
    slug = re.sub(r'[^A-Za-z0-9]+', '_', core).strip('_').lower()[:maxlen].rstrip('_')
    if len(re.sub(r'[^a-z]', '', slug)) >= 2:
        return slug
    fmt = "_".join(re.findall(r'%[0-9]*[dscx]', text)) or "blank"
    return ("fmt_" + re.sub(r'[^A-Za-z0-9]+', '_', fmt)).strip('_').lower()


def slug_plain(s, addr):
    """koei-snes data_walk: every non-alphanumeric to `_`, 28 chars, else str_<ADDR>."""
    w = "".join(c if c.isalnum() else "_" for c in s.strip().lower()).strip("_")
    w = "_".join(p for p in w.split("_") if p)[:28]
    return w or ("str_%04X" % addr)
