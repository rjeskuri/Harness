// The smoke test: the test runner works and package.json names the project.
// Replace it with real tests as the code grows.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

test("package.json names the project", () => {
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(pkg.name, "{{pkg}}");
});
