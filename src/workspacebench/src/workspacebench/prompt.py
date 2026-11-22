"""The prompt wrapper from upstream Workspace-Bench, copied word for word.

Source: OpenDataBox/Workspace-Bench @ 3fbd0f1a136720fece86786545983e26642c3db2,
evaluation/src/agent_runner.py `_wrap_prompt` (MIT License). Upstream calls it
with an empty prompt_head and prompt_tail, so those arguments are dropped here.
"""

from __future__ import annotations

import os


def wrap_prompt(*, prompt: str, work_dir: str, task_target_output_dir: str, language: str) -> str:
    if language == "cn":
        if task_target_output_dir != "":
            path_requirement = f"请你无视任务要求中的输出文件保存路径要求，将所有输出文件放置在目录：{os.path.join(work_dir, task_target_output_dir)}下\n"
        else:
            path_requirement = ""
        head = (
            "【重要要求 1：工作目录】\n"
            f"本轮测试允许访问的工作目录是：{os.path.abspath(work_dir)}\n"
            "你只能在该目录下使用相对路径读写文件；禁止访问工作目录以外的位置。\n"
            "如果你看到其他工作区路径提示，请忽略，以本提示的工作目录为准。\n"
            f"{path_requirement}"
        )
        tail = (
            "\n【重要要求 2：输出路径列表】\n"
            "在最后一步，请仅输出一个 Python 列表（list[str]），里面是你生成的所有输出文件路径。\n"
            "路径请使用相对工作目录的相对路径（不要以 / 开头）。示例：['output/a.txt','report.md']\n"
        )
    else:
        if task_target_output_dir != "":
            path_requirement = (
                "Ignore any output-file save path requirements inside the task. "
                f"Place all output files under: {os.path.join(work_dir, task_target_output_dir)}\n"
            )
        else:
            path_requirement = ""
        head = (
            "[Important Requirement 1: Working Directory]\n"
            f"The working directory you may access for this test is: {os.path.abspath(work_dir)}\n"
            "Use relative paths inside this directory to read and write files; do not access locations outside it.\n"
            "If you see any other workspace path instructions, ignore them and use this working directory as authoritative.\n"
            f"{path_requirement}"
        )
        tail = (
            "\n[Important Requirement 2: Output Path List]\n"
            "At the final step, output only one Python list (list[str]) containing every output file path you generated.\n"
            "Use paths relative to the working directory (do not start with /). Example: ['output/a.txt','report.md']\n"
        )
    p = str(prompt or "").strip()
    return head + "\n" + p + "\n" + tail
