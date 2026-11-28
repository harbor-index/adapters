import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopsbench.build_context import isolate_build_context


class BuildContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def environment(self, name, dockerfile="FROM scratch\nCOPY base/ /workspace/\n"):
        env = self.root / name
        (env / "base").mkdir(parents=True)
        (env / "Dockerfile").write_bytes(dockerfile.encode())
        return env

    def segment(self, name, data):
        env = self.environment(name)
        file = env / "base" / "module.txt"
        file.write_bytes(data)
        file.chmod(0o751)
        os.utime(file, ns=(0, 123456789))
        os.utime(file.parent, ns=(0, 234567890))
        return env, file

    def test_same_relative_path_mtime_and_size_with_different_bytes(self):
        first, old = self.segment("segment-01", b"old\n")
        second, new = self.segment("segment-02", b"new\n")
        self.assertEqual(old.relative_to(first), new.relative_to(second))
        self.assertEqual(old.stat().st_size, new.stat().st_size)
        self.assertEqual(old.stat().st_mtime_ns, new.stat().st_mtime_ns)

        isolate_build_context(first)
        isolate_build_context(second)
        first_source = (first / "Dockerfile").read_text().splitlines()[1].split()[1]
        second_source = (second / "Dockerfile").read_text().splitlines()[1].split()[1]
        self.assertNotEqual(first_source, second_source)
        self.assertFalse((first / second_source).exists())
        self.assertFalse((second / first_source).exists())
        self.assertFalse(old.exists())
        self.assertFalse(new.exists())
        self.assertEqual((first / first_source / old.name).read_bytes(), b"old\n")
        self.assertEqual((second / second_source / new.name).read_bytes(), b"new\n")

    def test_reproducible_and_preserves_file_directory_and_symlink_metadata(self):
        namespaces = []
        for name in ("one", "two"):
            env, file = self.segment(name, b"input\n")
            link = file.parent / "link"
            link.symlink_to("module.txt")
            dangling = file.parent / "dangling"
            dangling.symlink_to("missing")
            os.utime(link, ns=(0, 123), follow_symlinks=False)
            os.utime(dangling, ns=(0, 456), follow_symlinks=False)
            os.link(file, file.parent / "hardlink")
            os.utime(file.parent, ns=(0, 234567890))
            paths = (file, link, dangling, file.parent)
            metadata = [(path.lstat().st_mode, path.lstat().st_mtime_ns) for path in paths]
            isolate_build_context(env)
            # A second call verifies the generated layout and leaves it in place.
            isolate_build_context(env)
            namespace, = env.glob(".loopsbench-context-*")
            namespaces.append(namespace.name)
            for path, expected in zip(paths, metadata, strict=True):
                moved = namespace / path.relative_to(env)
                self.assertEqual((moved.lstat().st_mode, moved.lstat().st_mtime_ns), expected)
            moved = namespace / "base"
            self.assertEqual(os.readlink(moved / "link"), "module.txt")
            self.assertEqual(os.readlink(moved / "dangling"), "missing")
            self.assertTrue((moved / "hardlink").samefile(moved / file.name))
        self.assertEqual(*namespaces)

    def test_rewrites_only_local_copy_sources_and_keeps_ignore_rules(self):
        text = (
            "# syntax=docker/dockerfile:1\r\n"
            "FROM scratch\r\n"
            "RUN cat > /tmp/doc << 'EOF' && cat <<-\"MORE\"\r\n"
            "COPY pretend/ /workspace/\r\n"
            "EOF\r\n"
            "\tCOPY another/ /workspace/\r\n"
            "\tMORE\r\n"
            "COPY <<'INLINE' /inline\r\n"
            "COPY text/ /workspace/\r\n"
            "INLINE\r\n"
            "RUN printf '%s' \\\r\n"
            "# A comment in a continued instruction.\r\n"
            "    COPY elsewhere/ /workspace/\r\n"
            "RUN echo 'quoted <<NOT_A_HEREDOC'\r\n"
            "  copy\tbase/bin/tool  /workspace/bin/\r\n"
            "COPY requirements/ /workspace/requirements/"
        )
        env = self.environment("heredoc", text)
        (env / "base" / "bin").mkdir()
        (env / "base" / "bin" / "tool").touch()
        (env / "requirements").mkdir()
        original_ignore = b"# Retain these filters.\n/base/.git/\n**/__pycache__\n**/*.pyc\n**/.*\n"
        (env / ".dockerignore").write_bytes(original_ignore)
        (env / "task.yaml").write_text("instruction: local fixture\n")

        isolate_build_context(env)
        isolate_build_context(env)

        namespace, = env.glob(".loopsbench-context-*")
        prefix = namespace.name + "/"
        expected = text.replace("copy\tbase/bin/tool", f"copy\t{prefix}base/bin/tool")
        expected = expected.replace("COPY requirements/", f"COPY {prefix}requirements/")
        self.assertEqual((env / "Dockerfile").read_bytes(), expected.encode())
        self.assertTrue((namespace / "base" / "bin" / "tool").is_file())
        self.assertTrue((namespace / "requirements").is_dir())
        self.assertTrue((env / "task.yaml").is_file())
        self.assertEqual((env / ".dockerignore").read_bytes(), original_ignore)
        patterns = ["base/.git", "**/__pycache__", "**/*.pyc", "**/.*"]
        self.assertEqual(
            (env / "Dockerfile.dockerignore").read_text().splitlines(),
            patterns + [f"!{namespace.name}"] + [prefix + pattern for pattern in patterns],
        )

    def test_unsupported_input_leaves_environment_intact(self):
        instructions = (
            "ADD base/ /workspace/",
            'COPY ["base/", "/workspace/"]',
            "COPY . /workspace/",
            "COPY base/* /workspace/",
            "COPY $SOURCE /workspace/",
            "COPY --from=stage base/ /workspace/",
            "COPY base/ \\\n /workspace/",
            "RUN \\\n --mount=source=base,target=/mnt true",
            "ONBUILD COPY base/ /workspace/",
            "RUN <<'UNFINISHED'\nCOPY base/ /workspace/",
        )
        for number, instruction in enumerate(instructions):
            with self.subTest(instruction=instruction):
                text = "FROM scratch\nCOPY base/ /workspace/\n" + instruction + "\n"
                env = self.environment(str(number), text)
                with self.assertRaises(ValueError):
                    isolate_build_context(env)
                self.assertEqual((env / "Dockerfile").read_bytes(), text.encode())
                self.assertEqual({path.name for path in env.iterdir()}, {"Dockerfile", "base"})

    def test_refuses_copy_through_symlink_or_unsupported_ignore_file(self):
        env = self.environment("link", "FROM scratch\nCOPY base/link/item /workspace/\n")
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "item").touch()
        (env / "base" / "link").symlink_to(shared)
        with self.assertRaisesRegex(ValueError, "traverses a symlink"):
            isolate_build_context(env)
        self.assertTrue((env / "base" / "link").is_symlink())

        env = self.environment("ignore")
        ignore = env / ".dockerignore"
        ignore.write_text("base\n!base/module.txt\n")
        with self.assertRaisesRegex(ValueError, "unsupported ignore pattern"):
            isolate_build_context(env)
        self.assertTrue((env / "base").is_dir())
        self.assertFalse((env / "Dockerfile.dockerignore").exists())

    def test_failed_rewrite_rolls_back_and_can_be_retried(self):
        env, file = self.segment("retry", b"input\n")
        (env / ".dockerignore").write_text("base/.git\n")
        dockerfile = env / "Dockerfile"
        original = dockerfile.read_bytes()
        replace = Path.replace

        def fail_install(source, target):
            if target == dockerfile:
                raise OSError("simulated install failure")
            return replace(source, target)

        with patch.object(Path, "replace", fail_install):
            with self.assertRaisesRegex(OSError, "simulated install failure"):
                isolate_build_context(env)
        self.assertEqual(file.read_bytes(), b"input\n")
        self.assertEqual(dockerfile.read_bytes(), original)
        self.assertEqual({path.name for path in env.iterdir()}, {"Dockerfile", ".dockerignore", "base"})
        isolate_build_context(env)
        namespace, = env.glob(".loopsbench-context-*")
        self.assertTrue((namespace / file.relative_to(env)).is_file())


if __name__ == "__main__":
    unittest.main()
