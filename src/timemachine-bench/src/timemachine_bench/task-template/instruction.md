You are an experienced software engineer working on a Python project.
Your task is to migrate the code to an environment with newer versions of dependencies.
The repository is at `/work`, and the environment described below is already installed in this container.
Please make necessary but minimal changes on the repository to make all tests pass.
You can ignore any warnings in this task as they are not critical to the test results.

Run the tests with:

```bash
cd /work && bash {test_script}
```

The task is solved when this command exits with status 0.

Here are some information about the environment.

<python_version>
{python_version}
</python_version>

<dependency_versions>
{dependency_versions}
</dependency_versions>

Given below are some important rules to follow.
You must follow the rules in any case without exception.

### Rules

- Do not make changes to the code more than necessary.
  Your work is to make minimal changes on the repository to make all tests pass.
  You must not change any code that is not related to the test failures.
  This includes formatting, adding comments, and any kind of refactoring.
  You are not requested either to improve the code quality or implement new features.

- You must not change any test cases in a way that harms the original intent of the code.
  The following test files are read-only; changes to them, and to `{test_script}` and `{setup_script}`, are discarded when your work is graded:

<test_files>
{test_files}
</test_files>

- Do not try to create new files or directories.
  Only edits to files that already exist in `/work` are graded; new, deleted or renamed files are ignored.

- Do not install, upgrade or downgrade packages, and do not change anything outside `/work`.
  Your work is graded by running the tests in a fresh copy of this environment, where such changes do not exist.
