#!/usr/bin/env python3
"""Reword "since the fork" for an install that may not start at the fork.

cesspool.lol has every block from 961640 on, so its pages say "since the fork". An install behind a
pruned node starts where the node's blocks start, and the same sentences have to read "since block N"
there. This rewrites each of those phrases in cesspool/site.py and static/site.js into a call that
picks the words when the page is drawn (local.since() and its relatives, SINCE in site.js).

It is a script, not a set of edits by hand, so that it can be run again after merging upstream:
take upstream's side of any conflict in those two files, run this, and look at the diff. Running it
twice changes nothing.

    tools/since.py [--check]      --check lists what would change and exits 1 when anything would
"""
import io, os, re, sys, tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHRASES = (("since the fork", "local.since()"), ("Since the fork", "local.since_title()"))
# Whole sentences that name the height or the start of the chart, before the plain phrase is looked for.
WHOLE = (("since the fork at height {FORK}", "{local.from_the_fork()}"), ("From the fork to ", "{local.from_first()} to "))
# Strings that say what cesspool.lol saw on its own chain, which no install's start changes.
KEEP = ("every PowPeg spend since the fork reveals", '("all", "Since the fork", None, 30)')


def python(src):
    for old, new in WHOLE:
        src = src.replace(old, new)
    lines = src.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    at = lambda pos: starts[pos[0] - 1] + pos[1]
    edits = []  # (start, end, text)
    prev = None
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
            continue
        a, b = at(tok.start), at(tok.end)
        text = src[a:b]
        if tok.type == tokenize.FSTRING_MIDDLE:
            new = text
            for phrase, call in PHRASES:
                new = new.replace(phrase, "{" + call + "}")
            if new != text and not any(k in tok.line for k in KEEP):
                edits.append((a, b, new))
        elif tok.type == tokenize.STRING and any(p in text for p, _ in PHRASES):
            docstring = prev is None or prev.type in (tokenize.NEWLINE,) or (prev.type == tokenize.OP and prev.string == ":" and text.startswith(('"""', "'''")))
            if not docstring and not any(k in tok.line for k in KEEP):
                body = text.lstrip("rRbBuU")
                quote = body[:3] if body[:3] in ('"""', "'''") else body[0]
                inner = body[len(quote):-len(quote)]
                exact = dict(PHRASES).get(inner)
                if exact:
                    edits.append((a, b, exact))
                else:
                    inner = inner.replace("{", "{{").replace("}", "}}")
                    for phrase, call in PHRASES:
                        inner = inner.replace(phrase, "{" + call + "}")
                    edits.append((a, b, "f" + quote + inner + quote))
        prev = tok
    for a, b, new in sorted(edits, reverse=True):
        src = src[:a] + new + src[b:]
    return src


JS_HEAD = '''  // Where this install's blocks start, and the words for it: "since the fork" or "since block N".
  // site.py writes both on <body> for every page (local.body_attrs).
  var SINCE = document.body.getAttribute("data-since") || "since the fork";
  var FIRST = +document.body.getAttribute("data-fork") || 961640;
  var BRAND = document.body.getAttribute("data-brand") || "cesspool";
'''


def javascript(src):
    if "var SINCE = " not in src:
        src = src.replace('  "use strict";\n', '  "use strict";\n\n' + JS_HEAD, 1)
    head, body = src.split(JS_HEAD, 1)
    body = body.replace("since the fork", '" + SINCE + "')
    body = body.replace('· cesspool.lol"', '· " + BRAND')
    body = re.sub(r'cesspool covers every block from 961640 on\. (.*?) from before 961640, ',
                  r'This install covers blocks from " + FIRST + " on. \1 from before " + FIRST + ", ', body)
    body = body.replace(' + ""', "").replace('"" + ', "")
    return head + JS_HEAD + body


def main():
    check = "--check" in sys.argv[1:]
    changed = 0
    for rel, fix in (("cesspool/site.py", python), ("static/site.js", javascript)):
        path = os.path.join(ROOT, rel)
        with open(path) as f:
            old = f.read()
        new = fix(old)
        if new != old:
            changed += 1
            print(f"{rel}: {'would change' if check else 'reworded'}")
            if not check:
                with open(path, "w") as f:
                    f.write(new)
    if not changed:
        print("nothing to reword")
    sys.exit(1 if check and changed else 0)


if __name__ == "__main__":
    main()
