"""Upload a folder, build it on the desk, download the result.
A copy is resumable: run it again after an interruption and it continues.

    export GAIADESK_TOKEN_FILE=~/.config/gaiadesk/bot.token   # scopes: cp, exec
    python copy_a_file.py 123456789 ./site
"""

import sys

from gaiadesk import GaiaDesk, OperationFailedError

desk = sys.argv[1]
folder = sys.argv[2] if len(sys.argv) > 2 else "./site"
gd = GaiaDesk()

try:
    # A trailing / means "into that folder"; relative desk paths are under the desk user's home.
    up = gd.upload(folder, desk, "builds/", recursive=True)
    print("uploaded %d files, %d bytes (%d resumed) to %s" % (up["files"], up["bytes"], up["resumed_bytes"], up["destination"]))
except OperationFailedError as e:
    # Some files failed; the summary says which. Running the same copy again resumes.
    for f in e.json["failed"]:
        print("failed: %s: %s" % (f["path"], f["message"]), file=sys.stderr)
    sys.exit(1)

gd.exec(desk, ["tar", "-czf", "builds/site.tgz", "-C", "builds", "site"], shell="none", check=True)

down = gd.download(desk, "builds/site.tgz", "./site.tgz")
print("downloaded %d bytes to %s" % (down["bytes"], down["destination"]))
