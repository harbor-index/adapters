# Vendored olmOCR-Bench test code

The files in this directory are copied, unmodified, from the original
olmOCR-Bench harness so that pages are graded by exactly the upstream logic.

- Source: https://github.com/allenai/olmocr
- Commit: `f7cfe4c22098b154c76b6ec950d1c0a464eecf8d` (2026-03-25, package version 0.4.27)
- License: Apache License 2.0 (`LICENSE`, copied from the repository root)

| File here | Upstream path | SHA-256 |
|-----------|---------------|---------|
| `olmocr/__init__.py` | `olmocr/__init__.py` | `4466ba72645ddde5f8f4dc286eb8602c4197d2d194f0bbe61aa6f36c757b7743` |
| `olmocr/version.py` | `olmocr/version.py` | `d21af9d7bd593378f0ef8e8c04cedd20785f6a18e62f8d9478be871083ee474f` |
| `olmocr/repeatdetect.py` | `olmocr/repeatdetect.py` | `29d7ef1fc2a2fa7259eba40a00fa7eca355634f8f4f1af6c086682c8164ae317` |
| `olmocr/bench/__init__.py` | `olmocr/bench/__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `olmocr/bench/tests.py` | `olmocr/bench/tests.py` | `ff36b3cdf81c4a94f3e43cb78de8a31607eb6a0d140d0bfbfb2fe44122b55727` |
| `olmocr/bench/table_parsing.py` | `olmocr/bench/table_parsing.py` | `f120f7a207f2f9ff99b7a5d7cc8b144250b60f4ce9bb160405228866d93dbb79` |
| `olmocr/bench/katex/__init__.py` | `olmocr/bench/katex/__init__.py` | `dc168cb576c0a62f3b173016bb02d5f4f11d0fcf373f23ac20e168d1261aa5c9` |
| `olmocr/bench/katex/render.py` | `olmocr/bench/katex/render.py` | `7e04305b57255013362ecc43e183ccd319fb187d1b900c941df575b871c9df0b` |
| `olmocr/bench/katex/katex.min.js` | `olmocr/bench/katex/katex.min.js` | `863811e2baa0849c77bc92d26d44f4d0a4843c2a8ab52c462017dea57316e4d8` |
| `olmocr/bench/katex/katex.min.css` | `olmocr/bench/katex/katex.min.css` | `f787891b550d554c214aa8902f39ac46df2dbd48fdec500a2040a5dce1e8ab58` |

Notes:

- `katex.min.js` and `katex.min.css` are the KaTeX 0.16.21 distribution files
  that upstream bundles (identical to the official release; MIT license in
  `LICENSE-KaTeX`). They are only needed to render equations, so the three
  KaTeX files are shipped only with pages that have `math` tests.
- `olmocr/bench/benchmark.py` (the upstream command-line driver) is not
  vendored. `../evaluate.py` reproduces its per-PDF behaviour: test loading
  through `load_tests`, the default `BaselineTest`, and pass/fail scoring.
- To verify the copies: `sha256sum` each file and compare with the table, or
  diff against a checkout of the commit above.
