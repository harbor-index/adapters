"""Harbor adapter for Health Agent Environment (HAEnv) — Pack 1 Diagnosis (M2).

Generates Harbor task directories from an audited deterministic HAEnv M2 pack.
Each generated task runs:
  - `main`: isolated agent container with only the `haenv-ehr` CLI client (no gold/data).
  - `ehr-server`: Docker Compose sidecar serving gold-stripped records and budgeted workup
    plus an authenticated verifier grading endpoint running upstream HAEnv's exact
    S = G * D * (1 + Q) / 2 pipeline.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


UPSTREAM_REPO_URL = "https://github.com/thetahealth/mirobody-env.git"
UPSTREAM_PIN = "94761fb"
DEFAULT_PACK_SIZE = 100


class HealthAgentEnvironmentAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "diagnosis",
        n_items: int = DEFAULT_PACK_SIZE,
        seed: str | None = None,
        lang: str = "en",
        pack_dir: Path | None = None,
        upstream_repo: Path | None = None,
        strict_oracle: bool = True,
        **kwargs,
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.n_items = n_items
        self.seed = seed
        self.lang = lang
        self.pack_dir = Path(pack_dir) if pack_dir else None
        self.upstream_repo = Path(upstream_repo) if upstream_repo else None
        self.strict_oracle = strict_oracle
        self.template_dir = Path(__file__).resolve().parent / "task-template"
        self.extract_script = Path(__file__).resolve().parent / "extract_pack.py"

    @staticmethod
    def _sanitize_id(case_id: str) -> str:
        norm = re.sub(r"[^a-z0-9]+", "-", case_id.strip().lower()).strip("-")
        return norm or "task"

    @classmethod
    def _task_dirname(cls, case_id: str) -> str:
        return f"haenv-m2-{cls._sanitize_id(case_id)}"

    def _find_batch_dir(self, pack_dir: Path) -> Path:
        if (pack_dir / "cases.jsonl").is_file():
            return pack_dir
        manifest = pack_dir / "make_pack.json"
        if manifest.is_file():
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            pack_batch = meta.get("pack_batch")
            if pack_batch:
                p = Path(pack_batch)
                if not p.is_absolute():
                    p = pack_dir / p
                if (p / "cases.jsonl").is_file():
                    return p
        candidates = sorted((pack_dir / "pack" / "results").glob("*/*/*/cases.jsonl"))
        if not candidates:
            candidates = sorted(pack_dir.rglob("cases.jsonl"))
        if not candidates:
            raise FileNotFoundError(f"Could not find cases.jsonl under {pack_dir}")
        return candidates[-1].parent

    def _ensure_upstream_repo(self, work_root: Path) -> Path:
        if self.upstream_repo and (self.upstream_repo / "pyproject.toml").is_file():
            return self.upstream_repo
        repo_dir = work_root / "mirobody-env"
        if not (repo_dir / "pyproject.toml").is_file():
            subprocess.run(
                ["git", "clone", "--depth", "1", UPSTREAM_REPO_URL, str(repo_dir)],
                check=True,
            )
            subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", UPSTREAM_PIN],
                cwd=repo_dir,
                check=False,
            )
            subprocess.run(
                ["git", "checkout", UPSTREAM_PIN],
                cwd=repo_dir,
                check=False,
            )
        return repo_dir

    def _ensure_pack_batch(self, work_root: Path) -> tuple[Path, Path | None]:
        if self.pack_dir is not None:
            return self._find_batch_dir(self.pack_dir), self.upstream_repo

        repo_dir = self._ensure_upstream_repo(work_root)
        pack_out = work_root / f"m2-pack-{self.n_items}"
        if not (pack_out / "make_pack.json").is_file():
            cmd = [
                "uv",
                "run",
                "--with",
                "scikit-learn",
                "--with",
                "joblib",
                "python",
                "tools/generation/make_pack.py",
                "--pack",
                "m2",
                "--n",
                str(self.n_items),
                "--out",
                str(pack_out),
                "--gen-workers",
                "4",
            ]
            if self.seed is not None:
                cmd.extend(["--seed", self.seed])
            subprocess.run(cmd, cwd=repo_dir, check=True)
        return self._find_batch_dir(pack_out), repo_dir

    def _extract_cases(self, batch_dir: Path, repo_dir: Path | None, out_jsonl: Path) -> list[dict]:
        if repo_dir is not None and (repo_dir / "pyproject.toml").is_file():
            cmd = ["uv", "run", "--directory", str(repo_dir), "python", str(self.extract_script)]
        else:
            cmd = [
                "uv",
                "run",
                "--no-project",
                "--with",
                f"haenv @ git+{UPSTREAM_REPO_URL}@{UPSTREAM_PIN}",
                "python",
                str(self.extract_script),
            ]

        cmd.extend(
            [
                "--batch-dir",
                str(batch_dir),
                "--out-jsonl",
                str(out_jsonl),
                "--lang",
                self.lang,
            ]
        )
        if self.limit is not None:
            cmd.extend(["--limit", str(self.limit)])
        if self.task_ids:
            cmd.append("--task-ids")
            cmd.extend(self.task_ids)
        if self.strict_oracle:
            cmd.append("--strict-oracle")

        subprocess.run(cmd, check=True)
        return [
            json.loads(line)
            for line in out_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _verifier_token(case_id: str, case_sha16: str) -> str:
        # Deterministic per-case secret token shared only between docker-compose (ehr-server)
        # and tests/verify_token.txt (mounted only at verifier time). Never inside main image.
        return hashlib.sha256(f"haenv-harbor-verifier|{case_id}|{case_sha16}".encode("utf-8")).hexdigest()[:32]

    def _render_task(self, row: dict) -> None:
        cid = row["case_id"]
        sanitized = self._sanitize_id(cid)
        task_dirname = self._task_dirname(cid)
        task_dir = self.output_dir / task_dirname

        if task_dir.exists() and not self.overwrite:
            return
        if task_dir.exists():
            shutil.rmtree(task_dir)

        (task_dir / "environment" / "ehr").mkdir(parents=True)
        (task_dir / "solution").mkdir(parents=True)
        (task_dir / "tests").mkdir(parents=True)

        token = self._verifier_token(cid, row["case_sha16"])
        lang = row.get("language", self.lang)

        # 1. task.toml
        task_toml_tpl = (self.template_dir / "task.toml").read_text(encoding="utf-8")
        (task_dir / "task.toml").write_text(
            task_toml_tpl.replace("{task_id}", sanitized),
            encoding="utf-8",
        )

        # 2. instruction.md
        inst_tpl = (self.template_dir / "instruction.md").read_text(encoding="utf-8")
        (task_dir / "instruction.md").write_text(
            inst_tpl.replace("{question}", row["rendered_question"]),
            encoding="utf-8",
        )

        # 3. environment/ (main agent container + isolated ehr-server sidecar)
        shutil.copy2(
            self.template_dir / "environment" / "Dockerfile",
            task_dir / "environment" / "Dockerfile",
        )
        shutil.copy2(
            self.template_dir / "environment" / "haenv_ehr.py",
            task_dir / "environment" / "haenv_ehr.py",
        )
        (task_dir / "environment" / "haenv_ehr.py").chmod(0o755)

        compose_tpl = (self.template_dir / "environment" / "docker-compose.yaml").read_text(encoding="utf-8")
        (task_dir / "environment" / "docker-compose.yaml").write_text(
            compose_tpl.replace("{verifier_token}", token).replace("{language}", lang),
            encoding="utf-8",
        )

        shutil.copy2(
            self.template_dir / "environment" / "ehr" / "Dockerfile",
            task_dir / "environment" / "ehr" / "Dockerfile",
        )
        shutil.copy2(
            self.template_dir / "environment" / "ehr" / "ehr_server.py",
            task_dir / "environment" / "ehr" / "ehr_server.py",
        )
        (task_dir / "environment" / "ehr" / "case_public.json").write_text(
            json.dumps(row["case_public"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # 4. solution/solve.sh (real deterministic oracle)
        orders = row.get("oracle_orders") or []
        if orders:
            quoted = " ".join(shlex.quote(str(t)) for t in orders)
            order_commands = f"haenv-ehr order-test {quoted} > /dev/null"
        else:
            order_commands = "haenv-ehr get-profile > /dev/null"

        oracle_json = json.dumps(row["oracle_answer"], ensure_ascii=False, indent=2)
        solve_tpl = (self.template_dir / "solution" / "solve.sh").read_text(encoding="utf-8")
        solve_path = task_dir / "solution" / "solve.sh"
        solve_path.write_text(
            solve_tpl.replace("{order_commands}", order_commands).replace("{oracle_answer_json}", oracle_json),
            encoding="utf-8",
        )
        solve_path.chmod(0o755)

        # 5. tests/ (hidden gold + verifier token + verify.py + test.sh)
        test_sh = task_dir / "tests" / "test.sh"
        shutil.copy2(self.template_dir / "tests" / "test.sh", test_sh)
        test_sh.chmod(0o755)

        shutil.copy2(self.template_dir / "tests" / "verify.py", task_dir / "tests" / "verify.py")
        (task_dir / "tests" / "verify.py").chmod(0o755)

        (task_dir / "tests" / "gold_case.json").write_text(
            json.dumps(row["gold_case"], ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (task_dir / "tests" / "verify_token.txt").write_text(token + "\n", encoding="utf-8")

    def run(self) -> None:
        if self.split not in ("diagnosis", "parity"):
            raise ValueError(f"Unsupported --split {self.split!r}; expected 'diagnosis' or 'parity'")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="haenv_adapter_") as tmpdir:
            work_root = Path(tmpdir)
            batch_dir, repo_dir = self._ensure_pack_batch(work_root)
            extracted_jsonl = work_root / "extracted_cases.jsonl"
            rows = self._extract_cases(batch_dir, repo_dir, extracted_jsonl)

            for row in rows:
                self._render_task(row)

        print(f"Generated {len(rows)} HAEnv M2 diagnosis tasks in {self.output_dir}")
