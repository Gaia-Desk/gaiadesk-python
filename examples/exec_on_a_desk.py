"""Run one command on a desk and handle every outcome.

    export GAIADESK_TOKEN_FILE=~/.config/gaiadesk/bot.token   # a token with the `exec` scope
    python exec_on_a_desk.py 123456789 "df -h /"
"""

import sys

from gaiadesk import GaiaDesk, RefusedError, UnreachableError, UsageError


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python exec_on_a_desk.py <desk-id> [command line]", file=sys.stderr)
        return 2
    desk = sys.argv[1]
    command = sys.argv[2] if len(sys.argv) > 2 else "hostname"
    gd = GaiaDesk()  # credentials from the environment (GAIADESK_TOKEN_FILE)

    # Online is presence; a probe proves a command will actually get through.
    row = gd.probe(desk)
    if row.get("reachable") is False:
        kind = (row.get("probe") or {}).get("kind") or (row.get("last_failure") or {}).get("kind")
        print("desk %s is not reachable: %s" % (desk, kind), file=sys.stderr)
        return 1

    try:
        r = gd.exec(desk, command, shell="sh", timeout=120)
    except RefusedError as e:  # e.g. the token lacks the exec scope
        print("refused:", e, file=sys.stderr)
        return 254
    except UnreachableError as e:
        print("unreachable (%s): %s" % (e.kind, e), file=sys.stderr)
        return 255
    except UsageError as e:  # e.g. no credential set
        print("usage:", e, file=sys.stderr)
        return 255
    sys.stdout.write(r["stdout"])
    sys.stderr.write(r["stderr"])
    print("exit %d via %s in %d ms (shell: %s)" % (r["exit"], r["route"], r["duration_ms"], r["shell"]), file=sys.stderr)
    return r["exit"]


if __name__ == "__main__":
    sys.exit(main())
