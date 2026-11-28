# TimeMachine-bench task: __REPO_NAME__ @ __COMMIT_HASH__
#
# Rebuilds the upstream per-task image (the record's `dockerfile` field):
# Python 3.12.11, the repository at its benchmark commit with the record's
# patches applied, and its dependencies resolved by the upstream setup script
# against a PyPI index frozen at __MIGRATION_DATE__. Upstream resolved its
# environments on x86_64, and some records resolve to different wheels on
# other architectures, so the image is built for linux/amd64.
FROM --platform=linux/amd64 __BASE_IMAGE__

# Upstream resolves dependencies through its date-filtered PyPI index
# (services/pypi-timemachine, a git submodule). The same server, at the
# submodule commit upstream pins, runs only while the setup script runs and is
# removed afterwards.
RUN git clone -q https://github.com/r-fujii/pypi-timemachine.git /opt/pypi-timemachine/src \
 && git -C /opt/pypi-timemachine/src checkout -q __PYPI_TIMEMACHINE_COMMIT__ \
 && python -m venv /opt/pypi-timemachine/venv \
 && /opt/pypi-timemachine/venv/bin/pip install -q --no-cache-dir --index-url https://pypi.org/simple \
      -r /opt/pypi-timemachine/src/requirements.txt \
 && /opt/pypi-timemachine/venv/bin/pip install -q --no-cache-dir --index-url https://pypi.org/simple \
      --no-deps /opt/pypi-timemachine/src

WORKDIR /work

# The repository at the benchmark commit, without git history
# (upstream agents/src/setup/init/01_clone_repo.sh).
RUN git init -q /work \
 && git -C /work fetch -q --depth 1 __REPO_URL__ __COMMIT_HASH__ \
 && git -C /work checkout -q FETCH_HEAD \
 && rm -rf /work/.git

# The record's `patch` (unpinned dependency manifests plus the setup and test
# scripts) and `test_patch`, applied as upstream 03_apply_patches.sh does.
# Upstream's agent edits a separate copy of the repository in exactly this
# state, which is kept here before any build step touches /work.
COPY timemachine/ /opt/timemachine/
RUN __APPLY_PATCHES__
RUN cp -a /work /opt/timemachine/work-pristine
__EXTRA_UPSTREAM_STEPS__
RUN bash /opt/timemachine/run_setup.sh __SETUP_SCRIPT__ __MIGRATION_DATE__

# Record whether the resolved packages equal the record's dependency_versions
# (reported in verifier/metrics.json). Then put /work back to its pre-build
# state: upstream mounts the agent's copy of the repository over /work for
# every test run, so files the build steps left in /work (build/, *.egg-info,
# edited manifests) are never seen by the agent or the tests. The installed
# environment is unchanged. Finally record the starting content of /work, so
# that the agent's edits can be collected for the separate verifier, and remove
# the build-only files.
RUN python3 -I /opt/timemachine/env_check.py \
 && rm -rf /work \
 && mv /opt/timemachine/work-pristine /work \
 && python3 -I /opt/timemachine/collect.py --write-baseline \
 && rm -rf /opt/pypi-timemachine /opt/timemachine/patches /opt/timemachine/run_setup.sh /root/.cache/pip
