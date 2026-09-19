import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const manifest = JSON.parse(await readFile(join(root, "manifest.json"), "utf8"));
const archive = join(
  root,
  "dist",
  `${manifest.id}-${manifest.version}-alpha.zip`
);
const installer = join(root, "dist", `install-${manifest.id}-${manifest.version}.zsh`);
const recovery = join(root, "dist", `recover-${manifest.id}.zsh`);
const releaseManifest = join(
  root,
  "dist",
  `${manifest.id}-${manifest.version}-release.json`
);

const first = {
  archive: await sha256(archive),
  installer: await sha256(installer),
  recovery: await sha256(recovery),
  releaseManifest: await sha256(releaseManifest)
};
execFileSync(process.execPath, [join(root, "scripts", "package-alpha.mjs")], {
  stdio: "ignore"
});
const second = {
  archive: await sha256(archive),
  installer: await sha256(installer),
  recovery: await sha256(recovery),
  releaseManifest: await sha256(releaseManifest)
};

assert.deepEqual(second, first, "Two builds from the same inputs must be byte-identical.");
console.log(
  JSON.stringify({
    archiveSha256: second.archive,
    installerSha256: second.installer,
    recoverySha256: second.recovery,
    releaseManifestSha256: second.releaseManifest,
    reproducible: true
  })
);

async function sha256(path) {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}
