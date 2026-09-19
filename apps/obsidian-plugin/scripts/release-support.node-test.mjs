import assert from "node:assert/strict";
import test from "node:test";

import {
  assertReleaseMetadata,
  expandInstallerTemplate
} from "./release-support.mjs";

const manifest = {
  id: "speech-capture",
  version: "0.1.25",
  minAppVersion: "1.11.4"
};

test("accepts one consistent release version", () => {
  assert.doesNotThrow(() =>
    assertReleaseMetadata(
      manifest,
      { version: "0.1.25" },
      { "0.1.25": "1.11.4" }
    )
  );
});

test("rejects package and compatibility version drift", () => {
  assert.throws(
    () =>
      assertReleaseMetadata(
        manifest,
        { version: "0.1.24" },
        { "0.1.25": "1.11.4" }
      ),
    /Version mismatch/
  );
  assert.throws(
    () =>
      assertReleaseMetadata(
        manifest,
        { version: "0.1.25" },
        { "0.1.25": "1.10.0" }
      ),
    /versions\.json/
  );
});

test("renders all installer tokens and rejects omissions", () => {
  assert.equal(
    expandInstallerTemplate("v=@@VERSION@@ h=@@HASH@@", {
      VERSION: "0.1.25",
      HASH: "abc123"
    }),
    "v=0.1.25 h=abc123"
  );
  assert.throws(
    () => expandInstallerTemplate("v=@@VERSION@@ h=@@HASH@@", { VERSION: "0.1.25" }),
    /Unresolved installer tokens/
  );
});
