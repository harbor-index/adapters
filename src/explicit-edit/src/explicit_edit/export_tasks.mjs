// Dump the upstream Explicit Edit task matrix as JSON on stdout.
//
// Usage (cwd must be the upstream checkout so `--import tsx` resolves):
//   node --import tsx <this file> <upstream-checkout>
//
// The upstream generator (src/suites/explicit-edit/fixtures.ts) is the single
// source of truth. This script only serializes it, so task bytes, prompts, and
// fixture hashes are exactly what the original benchmark runs.
import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.argv[2];
if (!root) throw Error("Usage: node --import tsx export_tasks.mjs <upstream-checkout>");

const fixtures = pathToFileURL(path.resolve(root, "src/suites/explicit-edit/fixtures.ts")).href;
const { explicitEditTasks } = await import(fixtures);

const tasks = explicitEditTasks().map((t) => ({
  id: t.id,
  category: t.category,
  scale: t.scale,
  scaleUnit: t.scaleUnit,
  language: t.language,
  variant: t.variant,
  prompt: t.prompt,
  input: t.input,
  expected: t.expected,
  fixtureSha256: t.fixtureSha256,
}));

process.stdout.write(JSON.stringify(tasks));
