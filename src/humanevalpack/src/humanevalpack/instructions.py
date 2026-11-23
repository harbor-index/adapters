"""Wording the adapter adds around the original prompt.

The original prompt is embedded verbatim in every ``instruction.md``. The text
here only tells an agent where to put its answer and how that answer is graded,
which the original setting (a model continuing the prompt) did not need to say.
Everything stated about grading describes what the original harness does; see
``task-template/tests/verify.py`` for the code it refers to.

Nothing in these strings names the benchmark, its dataset or its harness: an
agent that recognises the source can look the solution up instead of writing it.
"""

from __future__ import annotations

from dataclasses import dataclass

SOLUTION_DIR = "/app"
TOOLCHAIN_DIR = "/opt/toolchain"

REWARD_SENTENCE = "The solution counts as correct only if {condition}."


@dataclass(frozen=True)
class SubsetText:
    title: str
    summary: str
    action: str
    tests_phrase: str


SUBSET_TEXT = {
    "fixtests": SubsetText(
        title="Fix a bug in a {language_name} function",
        summary=(
            "The problem text below shows a {language_name} function that contains a bug, "
            "the unit tests that the correct function must pass, and the request. It ends "
            "by repeating the declaration: the corrected function is meant to be written "
            "as a continuation of that declaration."
        ),
        action="Fix the function.",
        tests_phrase="the unit tests shown in the problem text",
    ),
    "fixdocs": SubsetText(
        title="Fix a bug in a {language_name} function",
        summary=(
            "The problem text below shows a {language_name} function that contains a bug, "
            "together with the documentation comment that states what the function must "
            "do, and the request. It ends by repeating the declaration and its "
            "documentation: the corrected function is meant to be written as a "
            "continuation of them. No unit tests are provided."
        ),
        action="Fix the function.",
        tests_phrase="hidden unit tests",
    ),
    "synthesize": SubsetText(
        title="Implement a {language_name} function",
        summary=(
            "The problem text below gives the request and then the declaration and "
            "documentation of the {language_name} function to write: the function is "
            "meant to be written as a continuation of them."
        ),
        action="Implement the function.",
        tests_phrase="hidden unit tests",
    ),
}


@dataclass(frozen=True)
class LanguageText:
    declaration_extras: str
    exclusions: str
    grading: str
    tools: str


LANGUAGE_TEXT = {
    "python": LanguageText(
        declaration_extras=" (with its imports)",
        exclusions=(
            "Do not include the unit tests, example calls or other top-level code that is "
            "not part of the solution."
        ),
        grading=(
            "The grader takes the content of `{solution_path}`, appends {tests_phrase} and "
            "executes the result with Python 3.9.13. A block of common imports is placed in "
            "front of your code (`math`, `re`, `sys`, `copy`, `datetime`, `itertools`, "
            "`collections`, `heapq`, `statistics`, `functools`, `hashlib`, `numpy`, "
            "`numpy as np`, `string`, `from typing import *` and "
            "`from collections import *`), and the program has 10 seconds to finish. "
            + REWARD_SENTENCE.format(condition="every test passes")
        ),
        tools=(
            "Python 3.9.13 (`python3`) with NumPy 1.25.2 is installed here, so you can run "
            "your code before you finish."
        ),
    ),
    "js": LanguageText(
        declaration_extras="",
        exclusions=(
            "Do not include the unit tests or example calls, and do not print anything: any "
            "output on stdout or stderr counts as a failure."
        ),
        grading=(
            "The grader takes the content of `{solution_path}`, appends {tests_phrase} and "
            "runs the result with `node` (Node.js v16.14.0; the `js-md5` package can be "
            "loaded with `require`). The program must finish within 10 seconds and must not "
            "write anything to stdout or stderr (a failing `console.assert` writes to "
            "stderr). " + REWARD_SENTENCE.format(condition="that holds")
        ),
        tools="Node.js v16.14.0 is installed here, so you can run your code before you finish.",
    ),
    "java": LanguageText(
        declaration_extras=" (with its `import` lines and the enclosing `class Solution`)",
        exclusions=(
            "Do not include the unit tests, a `Main` class or a `main` method, and keep "
            "`class Solution` without the `public` modifier."
        ),
        grading=(
            "The grader takes the content of `{solution_path}`, appends {tests_phrase} (a "
            "`public class Main` whose `main` method runs the checks), saves both as one "
            "file `Main.java`, compiles it with `javac` and runs `java Main` (OpenJDK 18). "
            "Compiling and running must finish within about 10 seconds. "
            + REWARD_SENTENCE.format(condition="the program exits normally")
        ),
        tools=(
            "OpenJDK 18 (`javac`, `java`) is installed here, so you can compile and run "
            "your code before you finish."
        ),
    ),
    "go": LanguageText(
        declaration_extras=" (with the `import` block, if the problem text shows one)",
        exclusions="Do not include the unit tests, a `main` function or example calls.",
        grading=(
            "The grader builds one file `main_test.go` from `package main`, the imports of "
            "the tests (`testing` and `github.com/stretchr/testify/assert`), the content of "
            "`{solution_path}` and {tests_phrase}, and runs `go test` on it (Go 1.18.4, "
            "20 second limit). Your file may start with `package main` and may contain its "
            "own `import` declarations. Import exactly the packages your code uses: unused "
            "imports do not compile, and the grader only adds a missing import for `math`, "
            "`strings`, `fmt`, `strconv`, `time`, `bytes`, `regexp` and `sort`. "
            + REWARD_SENTENCE.format(condition="the tests pass")
        ),
        tools=(
            "Go 1.18.4 is installed here. It runs in GOPATH mode (`GO111MODULE=off`) with "
            "testify v1.8.0 available offline, so `go test some_test.go` works in any "
            "directory."
        ),
    ),
    "cpp": LanguageText(
        declaration_extras=" (with its `#include` lines)",
        exclusions=(
            "Do not include the unit tests or a `main` function: the grader discards "
            "everything from the first occurrence of the text `int main` in your file."
        ),
        grading=(
            "The grader takes the content of `{solution_path}`, puts a block of common "
            "headers in front of it (`using namespace std;` followed by `#include` lines for "
            "`stdlib.h`, `algorithm`, `cmath`, `math.h`, `numeric`, `stdio.h`, `vector`, "
            "`set`, `map`, `queue`, `stack`, `list`, `deque`, `boost/any.hpp`, `string`, "
            "`climits`, `cstring`, `iostream`, `sstream` and `fstream`), appends "
            "{tests_phrase} (which define `main`), compiles the result with "
            "`g++ -std=c++11 test.cpp -lcrypto -lssl` (g++ 11.4.0) and runs the binary. "
            "Compiling and running must finish within about 60 seconds. "
            + REWARD_SENTENCE.format(condition="the binary exits with status 0")
        ),
        tools=(
            "g++ 11.4.0 with the Boost headers and OpenSSL is installed here, so you can "
            "compile and run your code before you finish."
        ),
    ),
    "rust": LanguageText(
        declaration_extras=" (with `fn main(){}` and the `use` lines, exactly as shown)",
        exclusions="Do not include the unit tests or a test module.",
        grading=(
            "The grader takes the content of `{solution_path}`, appends {tests_phrase} (a "
            "`#[cfg(test)] mod tests` module), and builds and runs the result as the binary "
            "`test` of a cargo package with `cargo check` and `cargo test` (Rust 1.71.1, "
            'edition 2021, dependencies `rand = "0.4"`, `regex = "1"` and `md5 = "0.7.0"`, '
            "300 second limit). Before compiling, the grader adds `fn main(){}` if the file "
            "has no `fn main()` and adds every line of the declaration that does not occur "
            "in the file, so changing the `use` lines can produce duplicate imports. "
            + REWARD_SENTENCE.format(condition="the tests pass")
        ),
        tools=(
            "Rust 1.71.1 is installed here. `{toolchain_dir}/rust` is that cargo package "
            "with its dependencies already downloaded and compiled; to build and test "
            "offline, copy it (for example `cp -r {toolchain_dir}/rust /tmp/rust`) and put "
            "your code in `src/bin/test.rs` of the copy."
        ),
    ),
}
