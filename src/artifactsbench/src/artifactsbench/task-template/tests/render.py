"""Execute the original screenshot procedure in a credential-free subprocess."""

import sys
from pathlib import Path

sys.path.insert(0, "/opt/artifactsbench/src")
from utils import capture_html_screenshots

page, *images = sys.argv[1:]
capture_html_screenshots(page, images, num_screenshots=3)
# The upstream capture function returns None on both success and failure.
if not all(Path(path).is_file() for path in images):
    raise RuntimeError("Could not capture all three ArtifactsBench screenshots")
