"""Resolve each Lite bug's upstream `branch` field to a full commit SHA (mirrors upstream
`git rev-parse <branch>^{commit}` order: full SHA, tag, then abbreviated SHA via GitHub API)."""
import json, re, subprocess, sys, urllib.request
from pathlib import Path

root = Path(sys.argv[1])
systems = json.loads((root / "benchmark/systems.json").read_text())
out = {}
for bug_id in sorted(f"{p.parent.name}-{p.name}" for p in (root / "benchmark/bugs").glob("*/*") if p.is_dir()):
    system, _, num = bug_id.partition("-")
    ref = json.loads((root / f"benchmark/bugs/{system}/{num}/report.json").read_text())["branch"]
    url = systems[system]["url"]
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        sha, how = ref, "full-sha"
    else:
        ls = subprocess.run(["git", "ls-remote", url, f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}"],
                            capture_output=True, text=True, check=True).stdout.split("\n")
        refs = dict(reversed(l.split("\t")) for l in ls if l)
        if f"refs/tags/{ref}^{{}}" in refs:
            sha, how = refs[f"refs/tags/{ref}^{{}}"], "annotated-tag"
        elif f"refs/tags/{ref}" in refs:
            sha, how = refs[f"refs/tags/{ref}"], "tag"
        elif re.fullmatch(r"[0-9a-f]{7,39}", ref):
            repo = url.removeprefix("https://github.com/")
            data = json.load(urllib.request.urlopen(f"https://api.github.com/repos/{repo}/commits/{ref}"))
            sha, how = data["sha"], "abbrev-sha"
        else:
            raise SystemExit(f"cannot resolve {bug_id} {ref}")
        if f"refs/heads/{ref}" in refs:
            how += "+branch-name-collision"
    out[bug_id] = {"system": system, "repo_url": url, "upstream_ref": ref, "commit": sha, "resolved_as": how}
    print(bug_id, ref, sha, how, flush=True)
Path(sys.argv[2]).write_text(json.dumps(out, indent=2) + "\n")
