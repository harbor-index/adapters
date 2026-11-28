import os
import re
import tempfile
import subprocess
from pathlib import Path

from typing import List, Tuple, Dict, Union, Optional
from typing_extensions import Annotated

from langchain_core.tools import tool, InjectedToolArg

@tool
def list_dir(
    host_repo_dir: Annotated[str, InjectedToolArg],
    repo_name: Annotated[str, InjectedToolArg],
    dir_path: Optional[str] = "/work"
) -> str:
    """
    List the name of files and subdirectories under the specified directory (default to `/work`).

    Args:
        dir_path: The path of the directory to inspect (default to `/work`).

    Returns:
        A string containing the names of files and subdirectories under the specified directory.
    """

    try:
        p = Path(dir_path)

        if p.is_absolute() and not p.is_relative_to("/work"):
            raise ValueError(f"You cannot access directories outside /work")
        if not p.is_absolute() and ".." in p.parts:
            raise ValueError(f"You cannot access directories outside /work")

        rel_path = p.relative_to("/work") if p.is_absolute() else p
        host_dir = Path(host_repo_dir) / rel_path

        files = []
        subdirectories = []

        # reraise exception to hide host paths
        if not host_dir.exists():
            raise FileNotFoundError(f"The directory does not exist: {dir_path}")
        if not host_dir.is_dir():
            raise NotADirectoryError(f"The path is not a directory: {dir_path}")

        for entry in host_dir.iterdir():
            if entry.name.startswith('.'):
                # skip hidden files and directories
                continue

            if entry.is_dir():
                subdirectories.append(entry.name)
            elif entry.is_file():
                files.append(entry.name)

        # sort to make the order deterministic
        subdirectories = list(sorted([d + "/" for d in subdirectories]))
        files = list(sorted(files))

        if dir_path == "/work":
            # remove setup/test script from the list
            # `setup_{repo_name}.sh` and `test_{repo_name}.sh`
            files = [f for f in files if f not in {f"setup_{repo_name}.sh", f"test_{repo_name}.sh"}]

        content_lst = subdirectories + files

        return f"Found {len(files)} file(s) and {len(subdirectories)} subdirectory(s) under {dir_path}:\n\n" + f"<directory>\n" + "\n".join(content_lst) + "\n</directory>"
    except Exception as e:
        return f"Error listing the directory: {dir_path}. Here is the message: {str(e)}"

@tool
def search_dir(
    regex_pattern: str,
    host_repo_dir: Annotated[str, InjectedToolArg],
    dir_path: Optional[str] = "/work"
) -> str:
    """
    Search for the given regular expression in all files under `dir_path` and return the name of matching files.
    If `dir_path` is not specified, perform search under the `/work` directory.

    Args:
        regex_pattern: The regular expression pattern to search for.
        dir_path: The path of the directory to search (default to `/work`).

    Returns:
        A string containing the name of matching files and the number of matches in the file, up to a maximum of 50 files.
    """

    try:
        ptn = re.compile(regex_pattern)

        p = Path(dir_path)

        if p.is_absolute() and not p.is_relative_to("/work"):
            raise ValueError(f"You cannot access directories outside /work")
        if not p.is_absolute() and ".." in p.parts:
            raise ValueError(f"You cannot access directories outside /work")

        rel_path = p.relative_to("/work") if p.is_absolute() else p
        host_dir = Path(host_repo_dir) / rel_path

        if not host_dir.exists():
            raise FileNotFoundError(f"The directory does not exist: {dir_path}")
        if not host_dir.is_dir():
            raise NotADirectoryError(f"The path is not a directory: {dir_path}")

    except Exception as e:
        return f'Search failed for "{regex_pattern}" in the directory. Here is the message: {str(e)}'

    matches = []

    for fpath in Path(host_dir).rglob("*.py"):
        try:
            match_in_file = 0

            # set `newline = ""` to keep original newline characters
            with open(fpath, "r", newline="") as f:
                for line in f:
                    line = line.rstrip()

                    if ptn.search(line):
                        match_in_file += 1

            if match_in_file > 0:
                rel_path = fpath.relative_to(host_repo_dir)
                container_path = Path("/work") / rel_path
                matches.append((str(container_path), match_in_file))
        except Exception:
            # skip files that cannot be opened
            continue

    num_matches = len(matches)

    if not matches:
        return f'No matches found for "{regex_pattern}" in the directory.'

    if num_matches > 50:
        return 'More than 50 matches found for "{regex_pattern}" in the directory.' + \
            " Please write a more specific query to narrow down the results."

    result_str = f'Found {len(matches)} match(es) for "{regex_pattern}" in the directory:\n\n'
    result_str += "\n".join(f"{fpath} ({count} match(es))" for fpath, count in matches)

    return result_str

@tool
def search_file(
    regex_pattern: str,
    file_path: str,
    host_repo_dir: Annotated[str, InjectedToolArg]
) -> str:
    """
    Search for the given regular expression in the file at `file_path` and return the content of matching lines.

    Args:
        regex_pattern: The regular expression pattern to search for.
        file_path: The path to the file to search (startswith `/work`).

    Returns:
        A string containing the matching lines with their line numbers up to a maximum of 50 matches.
    """
    matches = []

    try:
        ptn = re.compile(regex_pattern)

        p = Path(file_path)

        if p.is_absolute() and not p.is_relative_to("/work"):
            raise ValueError(f"You cannot access directories outside /work")
        if not p.is_absolute() and ".." in p.parts:
            raise ValueError(f"You cannot access directories outside /work")

        rel_path = p.relative_to("/work") if p.is_absolute() else p
        host_path = Path(host_repo_dir) / rel_path

        if not host_path.exists():
            raise FileNotFoundError(f"The file does not exist: {file_path}")
        if not host_path.is_file():
            raise IsADirectoryError(f"The path is not a file: {file_path}")

        with open(host_path, "r", newline="") as f:
            for i, line in enumerate(f, 1):
                line = line.rstrip()

                if ptn.search(line):
                    matches.append((i, line))

        if not matches:
            return f'No matches found for "{regex_pattern}" in {file_path}.'

        num_matches = len(matches)

        if num_matches > 50:
            return 'More than 50 matches found for "{regex_pattern}" in {file_path}.' + \
                " Please write a more specific query to narrow down the results."

        result_str = f'Found {len(matches)} match(es) for "{regex_pattern}" in {file_path}:\n\n'
        result_str += "\n".join(f"{i}: {line}" for i, line in matches)

        return result_str

    except Exception as e:
        return f'Search failed for "{regex_pattern}" in {file_path}. Here is the message: {str(e)}'

@tool
def view_file(
    file_path: str,
    line_no: int,
    host_repo_dir: Annotated[str, InjectedToolArg]
) -> str:
    """
    Open the content at `file_path` and return the content.
    Show 50 lines before and after the specified line number.

    Args:
        file_path: The path of the file to read (startswith `/work`).
        line_no: The line number around which to show the content.

    Returns:
        A string containing the content of the files.
    """

    try:
        p = Path(file_path)

        if p.is_absolute() and not p.is_relative_to("/work"):
            raise ValueError(f"You cannot access directories outside /work")
        if not p.is_absolute() and ".." in p.parts:
            raise ValueError(f"You cannot access directories outside /work")

        rel_path = p.relative_to("/work") if p.is_absolute() else p
        host_path = Path(host_repo_dir) / rel_path

        if not host_path.exists():
            raise FileNotFoundError(f"The file does not exist: {file_path}")
        if not host_path.is_file():
            raise IsADirectoryError(f"The path is not a file: {file_path}")

        target_lines = []
        start_line, end_line = max(1, line_no - 50), line_no + 50

        with open(host_path, "r", newline="") as f:
            for i, line in enumerate(f, start=1):
                if start_line <= i <= end_line:
                    target_lines.append(line)

        total_lines = i

        content = "".join(f"{i}: {line}" for i, line in enumerate(target_lines, start=start_line)).rstrip()

        # add info about collapsed lines
        lines_before, lines_after = start_line - 1, total_lines - end_line

        if lines_before > 0:
            content = f"({lines_before} lines above)\n" + content
        if lines_after > 0:
            content = content + f"\n({lines_after} lines below)"

        return f"Here is the content of the file around line #{line_no}:\n\n" + f"<content>\n{content}\n</content>"

    except Exception as e:
        return f"Error opening the file: {file_path}. Here is the message: {str(e)}"

# note: the tool actually returns a dictionary for later revert,
# but we do not mention it in the docstring as it is not important to notify LLMs about it
@tool
def edit_file(
    file_path: str,
    start_line: int,
    end_line: int,
    replacement_text: str,
    host_repo_dir: Annotated[str, InjectedToolArg],
    test_files: Annotated[List[str], InjectedToolArg]
) -> Dict[str, Optional[str]]:
    """
    Make edits to the file at `file_path` by replacing the lines from `start_line` to `end_line` (inclusive) with `replacement_text`.
    Returns the updated parts of the file after editing.

    Args:
        file_path: The path of the file to edit (startswith `/work`).
        start_line: The line number from which to start the edit.
        end_line: The line number at which to end the edit.
        replacement_text: The text to replace the specified lines with.

    Returns:
        A string containing the result of the edit operation.
    """

    try:
        # remove the `/work` prefix
        p = Path(file_path)

        if p.is_absolute() and not p.is_relative_to("/work"):
            raise ValueError(f"You cannot access directories outside /work")
        if not p.is_absolute() and ".." in p.parts:
            raise ValueError(f"You cannot access directories outside /work")

        rel_path = p.relative_to("/work") if p.is_absolute() else p
        host_path = Path(host_repo_dir) / rel_path

        if not host_path.exists():
            raise FileNotFoundError(f"The file does not exist: {file_path}")
        if not host_path.is_file():
            raise IsADirectoryError(f"The path is not a file: {file_path}")

        rel_path_str = str(rel_path)

        if rel_path_str in test_files:
            return {
                "patch": None,
                "message": "Editing test files is not allowed. The file was kept unchanged.",
            }

        with open(host_path, "r", newline="") as f:
            original_lines = f.readlines()

        to_replace_content = "".join(original_lines[start_line - 1:end_line])

        newline = ""

        if to_replace_content.endswith("\r\n"):
            newline = "\r\n"
        elif to_replace_content.endswith("\n"):
            newline = "\n"

        # add newline if there is no newline at the end
        replacement_text = replacement_text.rstrip(newline) + newline

        updated_lines = (
            original_lines[:start_line - 1] +
            replacement_text.splitlines(keepends=True) +
            original_lines[end_line:]
        )

        updated_content = "".join(updated_lines)

        with tempfile.NamedTemporaryFile("w", newline="") as tmpf:
            tmp_path = tmpf.name
            tmpf.write(updated_content)
            tmpf.flush()

            # generate the patch using diff command
            diff_proc = subprocess.run(
                [
                    "diff", "-u",
                    "--label", str(rel_path),
                    str(host_path),
                    "--label", str(rel_path),
                    str(tmp_path)
                ],
                stdout=subprocess.PIPE,
                check=False
            )

        patch_content_bytes = diff_proc.stdout
        patch_content = patch_content_bytes.decode("utf-8")

        # apply the patch under `host_repo_dir`
        subprocess.run(
            ["patch", "-p0", "-d", str(host_repo_dir)],
            input=patch_content,
            text=True,
            check=True,
            capture_output=True
        )

        # show 50 lines before and after the affected lines
        new_end_line = start_line + len(replacement_text.splitlines(keepends=True)) - 1
        to_show_start_line = max(1, start_line - 50)
        to_show_end_line = new_end_line + 50

        diff_lines = updated_lines[to_show_start_line - 1:to_show_end_line]
        diff_content = "".join(f"{i}: {line}" for i, line in enumerate(diff_lines, start=to_show_start_line)).rstrip()

        return {
            "patch": patch_content,
            "message": f"Edit succeeded. Here is the updated part of the file.\n\n" + f"<content>\n{diff_content}\n</content>"
        }

    except Exception as e:
        return {
            "patch": None,
            "message": f"Edit failed. The file was kept unchanged. Here is the message: {str(e)}"
        }

@tool
def replace_all_in_file(
    file_path: str,
    regex_pattern: str,
    replacement_string: str,
    host_repo_dir: Annotated[str, InjectedToolArg],
    test_files: Annotated[List[str], InjectedToolArg]
) -> Dict[str, Optional[str]]:
    """
    Finds all occurrences of a regular expression pattern in the file at `file_path` and replaces them with `replacement_string`.
    Preferred over `edit_file` only in case when:
        1) the edits are simple find-and-replace, and
        2) the edits are repetitive (the same error occurs multiple times)

    Args:
        file_path: The path to the file to edit.
        regex_pattern: The regular expression pattern to find.
        replacement_string: The string to replace the matched patterns with.

    Returns:
        A string containing the result of the replacements
    """
    try:
        # remove the `/work` prefix
        p = Path(file_path)

        if p.is_absolute() and not p.is_relative_to("/work"):
            raise ValueError(f"You cannot access directories outside /work")
        if not p.is_absolute() and ".." in p.parts:
            raise ValueError(f"You cannot access directories outside /work")

        rel_path = p.relative_to("/work") if p.is_absolute() else p
        host_path = Path(host_repo_dir) / rel_path

        if not host_path.exists():
            raise FileNotFoundError(f"The file does not exist: {file_path}")
        if not host_path.is_file():
            raise IsADirectoryError(f"The path is not a file: {file_path}")

        rel_path_str = str(rel_path)

        if rel_path_str in test_files:
            return {
                "patch": None,
                "message": "Editing test files is not allowed. The file was kept unchanged."
            }

        with open(host_path, "r", newline="") as f:
            original_content = f.read()

        updated_content, num_replacements = re.subn(
            regex_pattern,
            replacement_string,
            original_content
        )

        if num_replacements == 0:
            return {
                "patch": None,
                "message": f'No matches found for "{regex_pattern}" in {file_path}. The file was kept unchanged.'
            }

        with tempfile.NamedTemporaryFile("w", newline="") as tmpf:
            tmp_path = tmpf.name
            tmpf.write(updated_content)
            tmpf.flush()

            # generate the patch using diff command
            diff_proc = subprocess.run(
                [
                    "diff", "-u",
                    "--label", str(rel_path),
                    str(host_path),
                    "--label", str(rel_path),
                    str(tmp_path)
                ],
                stdout=subprocess.PIPE,
                check=False
            )

        patch_content_bytes = diff_proc.stdout
        patch_content = patch_content_bytes.decode("utf-8")

        # apply the patch under `host_repo_dir`
        subprocess.run(
            ["patch", "-p0", "-d", str(host_repo_dir)],
            input=patch_content,
            text=True,
            check=True,
            capture_output=True
        )

        return {
            "patch": patch_content,
            "message": f'Replaced {num_replacements} occurrences of "{regex_pattern}" with "{replacement_string}" in {file_path}.'
        }

    except Exception as e:
        return {
            "patch": None,
            "message": f"Replacement failed. The file was kept unchanged. Here is the message: {str(e)}"
        }

@tool
def revert_last(
    last_patch: Annotated[Optional[Tuple[str, str]], InjectedToolArg],
    host_repo_dir: Annotated[str, InjectedToolArg]
) -> str:
    """
    Revert the last edit and return the updated parts of the affected files.

    Returns:
        A string containing the result of the revert operation.
    """

    def _extract_diff_lines_to_show(patch: str) -> List[Tuple[int, int]]:
        lines_to_show = []

        # extract affected lines from the left
        for diff_header in re.finditer(r"^@@ \-(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", patch, re.MULTILINE):
            diff_start = int(diff_header.group(1))
            # affected line count can be omitted (default to 1)
            num_affected_lines = int(diff_header.group(2) or 1)
            diff_end = diff_start + num_affected_lines - 1

            line_start = max(1, diff_start - 50)
            # may exceed the total number of lines, but it is handled later
            line_end = diff_end + 50

            lines_to_show.append((line_start, line_end))

        if not lines_to_show:
            return []

        sorted_lines_to_show = list(sorted(lines_to_show, key=lambda x: x[0]))

        diff_lines = [sorted_lines_to_show[0]]
        current_start, current_end = diff_lines[0]

        # merge overlapping ranges
        for start, end in sorted_lines_to_show[1:]:
            if start <= current_end:
                current_end = end
                diff_lines[-1] = (current_start, current_end)
            else:
                diff_lines.append((start, end))
                current_start, current_end = start, end

        return diff_lines

    def _format_diff_blocks(file_path: str, file_lines: List[str], diff_lines: List[Tuple[int, int]]) -> str:
        parts = [f"<path>{file_path}</path>", "<updated_contents>"]

        for start, end in diff_lines:
            target_lines = file_lines[start - 1:end]
            part_content = "".join(f"{i}: {line}" for i, line in enumerate(target_lines, start=start)).rstrip()
            parts.append(f"  <content>\n{part_content}\n  </content>")

        parts.append("</updated_contents>")

        return "\n".join(parts)

    if last_patch is None:
        return "No edit to revert. The file was kept unchanged."

    try:
        last_modified_path, patch_content = last_patch
        # remove the `/work` prefix
        rel_path = Path(last_modified_path).relative_to("/work")
        host_path = Path(host_repo_dir) / rel_path

        if not host_path.exists():
            raise FileNotFoundError(f"The file does not exist: {last_modified_path}")
        if not host_path.is_file():
            raise IsADirectoryError(f"The path is not a file: {last_modified_path}")

        # revert the patch under `host_repo_dir`
        subprocess.run(
            ["patch", "-R", "-p0", "-d", str(host_repo_dir)],
            input=patch_content,
            text=True,
            check=True,
            capture_output=True
        )

        with open(host_path, "r", newline="") as f:
            updated_lines = f.readlines()

        # get diff ranges to show
        diff_lines = _extract_diff_lines_to_show(patch_content)

        diff_block_str = _format_diff_blocks(last_modified_path, updated_lines, diff_lines)

        return "Revert succeeded. Here are the updated parts of the file.\n\n" + diff_block_str

    except Exception as e:
        return f"Revert failed. The file was kept unchanged. Here is the message: {str(e)}"

@tool
def execute_tests(host_repo_dir: Annotated[str, InjectedToolArg],
                  image_name: Annotated[str, InjectedToolArg],
                  sec_timeout: Annotated[int, InjectedToolArg],
                  mem_limit: Annotated[str, InjectedToolArg]) -> Dict[str, Union[str, Optional[int]]]:
    """
    Execute the tests and get the test log.
    Returns last 100 lines of the log and the exit status of the container.

    Returns:
        A dictionary with the following keys:
            test_result: A string containing the result of the test execution.
            full_log: The full test log.
            container_status: The exit status code of the container. None if the execution failed.
    """
    try:
        # [Harbor port] Upstream runs `docker run -v <repo>:/work <image>`, a
        # fresh container of the task image whose CMD is
        # `bash /work/test_<repo>.sh` (WORKDIR /work), killed after
        # `sec_timeout` seconds. Under Harbor the agent already runs inside the
        # task image, and these tools can only edit files under /work, so the
        # test script runs in place with the same time limit. Without
        # --foreground, timeout kills the whole process group, as removing the
        # container does upstream. The memory limit is the task container's.
        command = [
            "timeout",
            "-s",
            "KILL",
            str(sec_timeout),
            "bash",
            f"/work/{os.environ['TMB_TEST_SCRIPT']}",
        ]

        result = subprocess.run(
            command,
            cwd=host_repo_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            check=False
        )

        test_log = result.stdout
        container_status = result.returncode

        # truncate the log
        # extract 100 lines from the end
        lines = test_log.splitlines(keepends=True)
        num_lines = len(lines)

        lines_to_show = lines[-100:]
        start_line = num_lines - len(lines_to_show) + 1

        truncated_log = "".join(f"{i}: {line}" for i, line in enumerate(lines_to_show, start=start_line))
        lines_before = max(0, num_lines - 100)

        if lines_before > 0:
            truncated_log = f"({lines_before} lines above)\n" + truncated_log

        truncated_log = truncated_log.rstrip()

        return {
            "test_result": "Test execution completed. Here is the test log.\n\n" + f"<test_log>\n{truncated_log}\n</test_log>",
            "full_log": test_log,
            "container_status": container_status
        }

    except Exception:
        return {
            "test_result": "Test execution failed.",
            "full_log": None,
            "container_status": None
        }

    finally:
        # [Harbor port] no container to remove
        pass

@tool
def search_last_log(
    regex_pattern: str,
    last_log_path: Annotated[str, InjectedToolArg],
) -> str:
    """
    Query the log of the last test execution for the given regular expression and return matching lines.

    Args:
        regex_pattern: The regular expression pattern to search for.

    Returns:
        A string containing the matching lines with their line numbers up to a maximum of 50 matches.
    """
    matches = []

    try:
        ptn = re.compile(regex_pattern)

        with open(last_log_path, "r", newline="") as f:
            for i, line in enumerate(f, start=1):
                line = line.rstrip()

                if ptn.search(line):
                    matches.append((i, line))

        if not matches:
            return f'No matches found for "{regex_pattern}" in the last test log.'

        num_matches = len(matches)

        if num_matches > 50:
            return 'More than 50 matches found for "{regex_pattern}" in the last test log.' + \
                " Please write a more specific query to narrow down the results."

        result_str = f'Found {len(matches)} match(es) for "{regex_pattern}" in the last test log:\n\n'
        result_str += "\n".join(f"{i}: {line}" for i, line in matches[:50])

        return result_str

    except Exception:
        return f'Search failed for "{regex_pattern}" in the last test log.'

@tool
def view_last_log(
    line_no: int,
    last_log_path: Annotated[str, InjectedToolArg]
) -> str:
    """
    Open the log of the last test execution and return the content.
    Show 50 lines before and after the specified line number.

    Args:
        line_no: The line number around which to show the content.

    Returns:
        A string containing the content of the log.
    """

    # extract 50 lines before and after the specified line number
    start_line, end_line = max(1, line_no - 50), line_no + 50

    target_lines = []

    try:
        with open(last_log_path, "r", newline="") as f:
            for i, line in enumerate(f, start=1):
                if start_line <= i <= end_line:
                    target_lines.append(line)

        log_content = "".join(f"{i}: {line}" for i, line in enumerate(target_lines, start=start_line)).rstrip()

        return f"Here is the content of the last test log around line #{line_no}.\n\n" + f"<test_log>\n{log_content}\n</test_log>"

    except Exception:
        return f"Error opening the last test log."

FILE_TOOLS = [list_dir, search_dir, search_file, view_file, revert_last]
EDIT_TOOLS = [edit_file, replace_all_in_file]
MISC_TOOLS = []
TEST_TOOLS = [execute_tests]
LOG_TOOLS = [search_last_log, view_last_log]

BIND_TOOLS = FILE_TOOLS + EDIT_TOOLS + TEST_TOOLS
TOOLS = FILE_TOOLS + EDIT_TOOLS + MISC_TOOLS + TEST_TOOLS + LOG_TOOLS

LIST_DIR_TOOL_NAME = list_dir.name
REVERT_TOOL_NAME = revert_last.name
TEST_EXECUTION_TOOL_NAME = execute_tests.name

BIND_TOOL_NAMES = {tool.name for tool in BIND_TOOLS}
EDIT_TOOL_NAMES = {tool.name for tool in EDIT_TOOLS}
LOG_TOOL_NAMES = {tool.name for tool in LOG_TOOLS}
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
