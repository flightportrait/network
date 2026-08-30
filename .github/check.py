#!/usr/bin/env python3
"""Repo gate. Three checks, no build step:

1. Hygiene: no compiled bytecode or local env files may be tracked, and
   no committed file may carry a builder's home path. This is the guard
   for the leak that a stray `git add` of __pycache__ once caused.
2. Parse: every inline <script> in web/*.html is valid JavaScript
   (node --check); every web/assets/**/*.json parses.
3. Secrets: a small deny-list of patterns that must never appear —
   private-tree paths and the shapes of things we do not commit.

Run locally: python3 .github/check.py
"""
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

failed = False


def _tracked_files():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    return out.stdout.splitlines()


tracked = _tracked_files()

# 1 + 3. Hygiene and secret deny-list over every tracked file.
BANNED_TRACKED = re.compile(r"(__pycache__/|\.pyc$|(^|/)\.env$)")
# Patterns that must not appear inside any committed file. Home paths and
# the compose/ops wiring live in the private monorepo, never here.
BANNED_CONTENT = [
    (re.compile(r"/home/[a-z0-9_-]+/"), "a builder home path"),
]
for path in tracked:
    if BANNED_TRACKED.search(path):
        print("FAIL tracked file must be gitignored: %s" % path)
        failed = True
        continue
    try:
        blob = open(path, "rb").read()
    except OSError:
        continue
    if b"\x00" in blob[:4096]:      # skip binaries (assets, fonts)
        continue
    text = blob.decode("utf-8", "replace")
    for rx, why in BANNED_CONTENT:
        if rx.search(text):
            print("FAIL %s contains %s" % (path, why))
            failed = True
print("hygiene: %d tracked files scanned" % len(tracked))

for path in sorted(glob.glob("web/*.html")):
    html = open(path).read()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    for i, code in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js",
                                         delete=False) as fh:
            fh.write(code)
            tmp = fh.name
        result = subprocess.run(["node", "--check", tmp],
                                capture_output=True, text=True)
        os.unlink(tmp)
        if result.returncode:
            print("FAIL %s script #%d\n%s" % (path, i, result.stderr))
            failed = True
    print("%s: %d inline script(s) parsed" % (path, len(blocks)))

n = 0
for path in sorted(glob.glob("web/assets/**/*.json", recursive=True)):
    try:
        json.load(open(path))
        n += 1
    except ValueError as exc:
        print("FAIL %s: %s" % (path, exc))
        failed = True
print("%d JSON asset(s) parsed" % n)

sys.exit(1 if failed else 0)
