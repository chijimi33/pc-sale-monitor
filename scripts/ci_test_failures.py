"""Print failing tests as workflow annotations.

Job logs need a signed-in GitHub account, annotations do not.  This turns
the unittest output of a failed test step into error annotations so the
failing test and its traceback can be read from the public API.
"""
import os
import re
import subprocess
import sys

LIMIT = 8          # GitHub shows at most ten error annotations per step
TRACEBACK = 3000   # trailing characters of each failure block


def escape(text, title=False):
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(",", "%2C").replace("::", "%3A%3A") if title else text


def failures(text):
    blocks = re.split(r"^={70}\n", text, flags=re.M)[1:]
    for block in blocks:
        head, _, rest = block.partition("\n" + "-" * 70 + "\n")
        body = re.split(r"^-{70}\nRan \d+ tests? in ", rest, flags=re.M)[0].strip()
        if re.match(r"(FAIL|ERROR): ", head):
            yield head.strip().splitlines()[0], body


def main(path):
    with open(path, encoding="utf-8", errors="replace") as stream:
        text = stream.read()
    found = list(failures(text))
    for title, body in found[:LIMIT]:
        print("::error title=" + escape(title[:120], True) + "::" + escape(body[-TRACEBACK:]))
    if not found:
        # The run ended without a unittest report (crash, kill, import of the runner).
        print("::error title=test run ended without a failure report::" + escape(text[-TRACEBACK:]))
    try:
        git = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        git = "git unavailable"
    print("::notice title=test environment::" + escape(
        "%d failing test(s); image %s %s; %s; Python %s" % (
            len(found), os.environ.get("ImageOS", "?"), os.environ.get("ImageVersion", "?"),
            git, sys.version.split()[0])))


if __name__ == "__main__":
    main(sys.argv[1])
