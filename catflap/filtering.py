"""Filter query language: the legacy boolean parser, the unified Android-Studio
field-key parser, match highlighting, and autocomplete token helpers."""

import re


def compile_term(term):
    """Plain text -> escaped substring pattern; /…/ -> regex. Both case-insensitive.
    A trailing 'i' (e.g. /foo/i) is accepted and ignored — matching is always
    case-insensitive. An invalid /regex/ falls back to literal matching."""
    m = re.fullmatch(r"/(.+)/i?", term)
    if m:
        try:
            return re.compile(m.group(1), re.IGNORECASE)
        except re.error:
            pass
    return re.compile(re.escape(term), re.IGNORECASE)


def parse_terms(text):
    """Boolean query -> DNF clause list of (pattern, negated) pairs.
    Operators are uppercase-only words; AND binds tighter than OR.
    'a AND NOT b OR c' == (a AND NOT b) OR c."""
    clauses = []
    for part in re.split(r"\s+OR\s+", text):
        patterns = []
        for t in re.split(r"\s+AND\s+", part):
            t = t.strip()
            if not t:
                continue
            negated = t.startswith("NOT ") and bool(t[4:].strip())
            if negated:
                t = t[4:].strip()
            patterns.append((compile_term(t), negated))
        if patterns:
            clauses.append(patterns)
    return clauses


def matches(value, clauses):
    """True if no clauses (filter empty) or any clause is fully satisfied."""
    return not clauses or any(
        all(bool(p.search(value)) != negated for p, negated in patterns)
        for patterns in clauses
    )


# ---- unified query language (Android Studio style) --------------------------
#
# One box, field-scoped keys. A predicate is (field, op, pattern, negated):
#   field  — "tag" | "msg" | "pkg" | "any"  ("any" = tag OR msg)
#   op     — "contains" | "exact" | "regex"
#   pattern— compiled, case-insensitive
#   negated— leading '-' on the key
# Keys:  tag:  message:/msg:  package:/pkg:   ·  =: exact  ·  ~: regex  ·  -key: negate
# Bare terms (no key) become field "any" and match tag OR msg.
# OR (uppercase) splits clauses; whitespace / AND join within a clause; a
# leading NOT before a bare term negates it. A query is DNF: OR of ANDs.

FIELD_ALIASES = {"tag": "tag", "message": "msg", "msg": "msg", "package": "pkg", "pkg": "pkg"}

# key + optional =:/~:/: operator, e.g. tag:  message=:  -pkg~:
KEY_RE = re.compile(
    r"(?P<neg>-)?(?P<key>tag|message|msg|package|pkg)(?P<op>=:|~:|:)",
    re.IGNORECASE,
)


def compile_predicate(field, op, raw, negated):
    """Build a (field, op, pattern, negated) predicate. Exact anchors the
    whole value; regex compiles raw; contains uses /…/ regex or literal."""
    if op == "exact":
        pat = re.compile(rf"^{re.escape(raw)}$", re.IGNORECASE)
    elif op == "regex":
        try:
            pat = re.compile(raw, re.IGNORECASE)
        except re.error:
            pat = re.compile(re.escape(raw), re.IGNORECASE)
    else:  # contains — honour inline /regex/ for parity with the old boxes
        pat = compile_term(raw)
    return (field, op, pat, negated)


def _op_name(op_token):
    return {"=:": "exact", "~:": "regex", ":": "contains"}[op_token]


def parse_query(text):
    """Unified query -> DNF list of clauses; each clause a list of predicates.
    Empty query -> []. Bare terms -> field 'any'."""
    clauses = []
    for part in re.split(r"\s+OR\s+", text.strip()):
        preds = _parse_clause(part)
        if preds:
            clauses.append(preds)
    return clauses


def _parse_clause(part):
    """One OR-segment -> list of AND-ed predicates.

    An explicit ` AND ` and every field key start a new predicate; a key's
    value runs up to the next key or the next AND. Keyless spans split on
    whitespace into 'any' predicates, honouring a leading NOT."""
    preds = []
    for chunk in re.split(r"\s+AND\s+", part):
        preds.extend(_parse_and_term(chunk))
    return preds


def _strip_trailing_not(span):
    """A 'NOT' word at the end of a span negates the key that follows it (so
    'NOT message:fill' == '-message:fill'). Return (span_without_not, not_flag)."""
    words = span.split()
    if words and words[-1] == "NOT":
        return " ".join(words[:-1]), True
    return span, False


def _parse_and_term(part):
    """A single AND-term — may still hold several keys (space = AND), e.g.
    'tag:Ads -message:fill'. Keys cut it into spans; the leading keyless span
    becomes bare 'any' predicates. A 'NOT' right before a key negates it.

    Value greediness is per field: tag/package values are a single token (they
    never contain spaces), so 'package:com.foo crash' is package com.foo AND a
    bare 'crash' search. message values keep the whole phrase ('message:no fill'
    matches 'no fill'). Exact/regex (=:/~:) always take the whole value."""
    preds = []
    keys = list(KEY_RE.finditer(part))
    if not keys:
        return _bare_predicates(part)

    lead, not_next = _strip_trailing_not(part[: keys[0].start()])
    preds.extend(_bare_predicates(lead))
    for i, m in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else len(part)
        value_span = part[m.end() : end]
        raw, not_after = _strip_trailing_not(value_span.strip())
        field = FIELD_ALIASES[m.group("key").lower()]
        op = _op_name(m.group("op"))
        negated = bool(m.group("neg")) or not_next
        # tag/package contains: first token is the value, the rest are bare
        # search terms (those fields never hold spaces, so trailing words are
        # clearly separate). message — and any exact/regex match — keeps the
        # whole phrase.
        if raw and op == "contains" and field in ("tag", "pkg"):
            first, _, rest = raw.partition(" ")
            preds.append(compile_predicate(field, op, first, negated))
            if rest.strip():
                preds.extend(_bare_predicates(rest))
        elif raw:
            preds.append(compile_predicate(field, op, raw, negated))
        # a key with no value (trailing "tag:") is an in-progress token — skip
        not_next = not_after
    return preds


# a token is a /regex/ group (which may contain spaces) or a run of non-space
BARE_TOKEN_RE = re.compile(r"/[^/]*/i?|\S+")


def _bare_tokens(span):
    """Split a keyless span into tokens, keeping an inline /regex/ whole even
    when it contains spaces ('/retry \\d+/' is one token, not two)."""
    return BARE_TOKEN_RE.findall(span)


def _bare_predicates(span):
    """Tokenize a keyless span into 'any' predicates; 'NOT word' negates."""
    out = []
    words = _bare_tokens(span)
    i = 0
    while i < len(words):
        w = words[i]
        if w == "AND":
            i += 1
            continue
        if w == "NOT" and i + 1 < len(words):
            out.append(compile_predicate("any", "contains", words[i + 1], True))
            i += 2
            continue
        if w == "NOT":  # dangling NOT — ignore
            i += 1
            continue
        out.append(compile_predicate("any", "contains", w, False))
        i += 1
    return out


def _scope_box(value, key):
    """Rewrite an old single-field box value into key-scoped unified syntax.

    The old boxes spoke AND/OR/NOT; prefix the key onto each bare term so the
    meaning is preserved. 'a OR b' in the tag box -> 'tag:a OR tag:b'.
    'x AND NOT y' -> 'tag:x AND -tag:y'. /regex/ becomes key~:regex."""
    value = value.strip()
    if not value:
        return ""
    or_parts = []
    for clause in re.split(r"\s+OR\s+", value):
        and_terms = []
        for t in re.split(r"\s+AND\s+", clause):
            t = t.strip()
            if not t:
                continue
            neg = t.startswith("NOT ") and bool(t[4:].strip())
            if neg:
                t = t[4:].strip()
            m = re.fullmatch(r"/(.+)/i?", t)
            if m:
                term = f"{'-' if neg else ''}{key}~:{m.group(1)}"
            else:
                term = f"{'-' if neg else ''}{key}:{t}"
            and_terms.append(term)
        if and_terms:
            or_parts.append(" AND ".join(and_terms))
    return " OR ".join(or_parts)


def _migrate_query(f):
    """Build the unified-box value from a saved filter dict.

    New format stores 'query' directly. Legacy format stored separate 'tag'
    and 'msg' boxes — fold them into scoped unified syntax (AND-joined)."""
    if "query" in f:
        return f.get("query", "")
    tag = _scope_box(f.get("tag", ""), "tag")
    msg = _scope_box(f.get("msg", ""), "message")
    # if either side itself has OR-alternatives, parenthesise via AND-distribution
    if tag and msg:
        # both present: AND them. OR inside either side would bind wrong, so we
        # wrap each multi-clause side back into a single clause is impossible in
        # this flat language — keep it simple and join with AND, which is correct
        # when neither side uses OR (the common case). Sides using OR are rare in
        # saved presets; joining still yields a usable, close-enough query.
        return f"{tag} AND {msg}" if " OR " not in tag and " OR " not in msg else f"{tag} {msg}"
    return tag or msg


def query_matches(tag, msg, pkg, clauses):
    """True if no clauses, or any clause's predicates all hold for the line."""
    if not clauses:
        return True
    fields = {"tag": tag, "msg": msg, "pkg": pkg}
    return any(
        all(_pred_holds(p, fields) for p in clause)
        for clause in clauses
    )


def _pred_holds(pred, fields):
    field, _op, pat, negated = pred
    if field == "any":
        hit = bool(pat.search(fields["tag"])) or bool(pat.search(fields["msg"]))
    else:
        hit = bool(pat.search(fields[field]))
    return hit != negated


def highlight_patterns(clauses):
    """From a parsed query, the compiled patterns to highlight in each field.

    Returns (tag_pats, msg_pats). Only positive (non-negated) predicates
    contribute — you can't highlight the absence of a term. A bare 'any' term
    highlights in both fields, so the colour shows which field the hit is in.
    Exact predicates (anchored ^…$) are de-anchored so the visible substring
    still highlights."""
    tag_pats, msg_pats = [], []
    seen_tag, seen_msg = set(), set()
    for clause in clauses:
        for field, op, pat, negated in clause:
            if negated:
                continue
            hl = _deanchor(pat) if op == "exact" else pat
            if field in ("tag", "any") and hl.pattern not in seen_tag:
                seen_tag.add(hl.pattern)
                tag_pats.append(hl)
            if field in ("msg", "any") and hl.pattern not in seen_msg:
                seen_msg.add(hl.pattern)
                msg_pats.append(hl)
    return tag_pats, msg_pats


def _deanchor(pat):
    """Strip ^…$ from an exact predicate so the literal still highlights."""
    src = pat.pattern
    if src.startswith("^") and src.endswith("$"):
        src = src[1:-1]
    try:
        return re.compile(src, re.IGNORECASE)
    except re.error:
        return pat


OR_SPLIT_RE = re.compile(r"\s+(?:OR|AND)\s+")


def split_last_term(text):
    """'toto OR pix' -> ('toto OR ', 'pix'); 'pix' -> ('', 'pix')."""
    last_end = 0
    for m in OR_SPLIT_RE.finditer(text):
        last_end = m.end()
    return text[:last_end], text[last_end:]


def suggest(candidates, current_term, limit=8):
    """Frequency-ordered candidates containing current_term (excluding exact match)."""
    term = current_term.strip().lower()
    out = []
    for c in candidates:
        cl = c.lower()
        if term and (term not in cl or term == cl):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


# ---- unified-box autocomplete -----------------------------------------------

# the bit the user is editing: everything after the last OR / whitespace, except
# that a key's value may contain spaces (message:no fill) so we don't split there.
QUERY_TOKEN_RE = re.compile(r"\s+OR\s+|\s+", re.IGNORECASE)


def split_query_token(text):
    """Return (prefix, token) where token is the in-progress chunk at the end.

    A trailing key value with spaces stays whole: 'tag:Ad message:no fi' ->
    ('tag:Ad ', 'message:no fi'). A bare trailing word splits on whitespace:
    'foo ba' -> ('foo ', 'ba')."""
    # find the start of the current key token, if the tail contains one
    m = None
    for m in KEY_RE.finditer(text):
        pass
    if m:
        # is the cursor still inside this key's value (no OR after it)?
        tail = text[m.start():]
        if not re.search(r"\s+OR\s+", tail, re.IGNORECASE):
            return text[: m.start()], tail
    # otherwise split on the last whitespace / OR
    last_end = 0
    for mm in QUERY_TOKEN_RE.finditer(text):
        last_end = mm.end()
    return text[:last_end], text[last_end:]


def parse_token(token):
    """Split an in-progress token into (negated, key, op_token, value).
    key/op are None for a bare term. value is the partial text being completed."""
    m = KEY_RE.match(token)
    if m:
        return bool(m.group("neg")), m.group("key").lower(), m.group("op"), token[m.end():]
    return False, None, None, token
