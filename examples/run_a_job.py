"""Long work belongs in a background job: it keeps running after this script,
the connection, or GaiaDesk itself restarts. (Do not background work inside
exec: exec ends its whole process tree when it returns.)

    export GAIADESK_TOKEN_FILE=~/.config/gaiadesk/bot.token   # scope: jobs
    python run_a_job.py 123456789
"""

import sys
import time

from gaiadesk import GaiaDesk

desk = sys.argv[1]
gd = GaiaDesk()
name = "nightly-%d" % int(time.time())

# An argument list (see README "Known gaps": run re-quotes a single command line).
job = gd.run_job(desk, name, ["sh", "-c", "cd ~/src/app && make test"], priority="low", cpu=50, mem="4G", keep_awake=True)
print("started %s (pid %s); caps enforced as: %s" % (job["name"], job.get("pid"), job.get("enforcement", [])))

# Poll instead of following:
while True:
    j = next(j for j in gd.jobs(desk) if j["name"] == name)
    if j["state"] != "running":
        break
    print("%s: running, %d bytes of output so far" % (name, j.get("log_bytes", 0)))
    time.sleep(10)

print(gd.job_logs(desk, name, tail=4000), end="")

print("%s: %s exit %s" % (name, j["state"], j.get("exit_code")))
# To stop a job early: gd.kill_job(desk, name)
