import json

import pytest

from deepplanning_shopping.results import check_oracle


@pytest.fixture
def job(tmp_path):
    (tmp_path / "result.json").write_text(
        json.dumps(
            {"stats": {"n_completed_trials": 1, "n_errored_trials": 0, "n_retries": 0}}
        )
    )
    trial = tmp_path / "trial"
    logs = trial / "verifier"
    logs.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "exception_info": None,
                "verifier_environment_mode": "separate",
                "task_name": "qwen/example",
                "trial_name": "trial",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )
    (logs / "reward.txt").write_text("1\n")
    (logs / "report.json").write_text(
        json.dumps(
            {
                "is_completed": True,
                "case_score": 1.0,
                "matched_count": 5,
                "expected_count": 5,
            }
        )
    )
    return tmp_path


def test_accepts_complete_isolated_evidence(job):
    receipt = check_oracle(job, 1)
    assert receipt["match_rate"] == 1.0
    assert receipt["rewards"] == {"qwen/example": 1.0}


def test_rejects_missing_trials(job):
    with pytest.raises(ValueError, match="Expected 2"):
        check_oracle(job, 2)


def test_rejects_reward_evidence_disagreement(job):
    (job / "trial/verifier/reward.txt").write_text("0\n")
    with pytest.raises(ValueError, match="disagree"):
        check_oracle(job, 1)


def test_rejects_shared_verifier(job):
    path = job / "trial/result.json"
    trial = json.loads(path.read_text())
    trial["verifier_environment_mode"] = "shared"
    path.write_text(json.dumps(trial))
    with pytest.raises(ValueError, match="Invalid isolated verification"):
        check_oracle(job, 1)
