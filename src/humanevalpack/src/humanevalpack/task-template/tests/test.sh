#!/bin/bash
# Scores the solution file in /app with this problem's unit tests.
#
# verify.py assembles the solution with the unit tests and runs the result
# through the original harness's executor (execute.py, an unmodified copy). It
# exits with 0 when the solution passed and with 1 when it failed; any other
# status means that scoring itself broke.
#
# The reward file /logs/verifier/reward.txt is written here on every path and
# never read back: it starts as 0 and becomes 1 only after verify.py has
# reported a pass. The submitted code runs under an unprivileged account that
# cannot write to the log directory, cannot read the tests directory and cannot
# enter the directories whose content comes from the solving phase. None of its
# processes is left when verify.py returns.
set -u
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

LOG_DIR=/logs/verifier
REWARD_FILE=/logs/verifier/reward.txt

mkdir -p "$LOG_DIR"
chown root:root "$LOG_DIR" /tests
chmod 755 "$LOG_DIR"
chmod -R go-rwx /tests
# Only verify.py, which runs as root, reads the solution file.
for dir in /app /logs/artifacts; do
    if [ -d "$dir" ] && [ ! -L "$dir" ]; then
        chown root:root "$dir"
        chmod 700 "$dir"
    fi
done
rm -f "$LOG_DIR/reward.json" "$LOG_DIR/result.json"
echo 0 > "$REWARD_FILE"

/opt/toolchain/python/bin/python3 -I /tests/verify.py \
    --task /tests/task.json \
    --solution-dir /app \
    --log-dir "$LOG_DIR"
status=$?

if [ "$status" -eq 0 ]; then
    echo 1 > "$REWARD_FILE"
elif [ "$status" -eq 1 ]; then
    echo 0 > "$REWARD_FILE"
else
    # Scoring broke (this is not a verdict on the solution). The reward stays 0
    # and no result file is kept, which marks the trial as unscored.
    echo "SCORING ERROR: verify.py exited with status $status; reward set to 0" >&2
    rm -f "$LOG_DIR/result.json"
    echo 0 > "$REWARD_FILE"
fi

exit "$status"
