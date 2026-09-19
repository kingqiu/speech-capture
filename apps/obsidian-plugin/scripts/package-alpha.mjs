import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  chmod,
  cp,
  mkdir,
  readFile,
  rm,
  stat,
  utimes,
  writeFile
} from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  assertReleaseMetadata,
  expandInstallerTemplate
} from "./release-support.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const manifest = JSON.parse(await readFile(join(root, "manifest.json"), "utf8"));
const packageJson = JSON.parse(await readFile(join(root, "package.json"), "utf8"));
const versions = JSON.parse(await readFile(join(root, "versions.json"), "utf8"));
assertReleaseMetadata(manifest, packageJson, versions);

const outputRoot = join(root, "dist");
const packageDirectory = join(outputRoot, manifest.id);
const archiveName = `${manifest.id}-${manifest.version}-alpha.zip`;
const archive = join(outputRoot, archiveName);
const checksumFile = `${archive}.sha256`;
const installerName = `install-${manifest.id}-${manifest.version}.zsh`;
const installer = join(outputRoot, installerName);
const releaseManifestName = `${manifest.id}-${manifest.version}-release.json`;
const releaseManifestPath = join(outputRoot, releaseManifestName);
const releaseFiles = ["main.js", "manifest.json", "styles.css"];
const reproducibleTimestamp = new Date("2000-01-01T00:00:00.000Z");

await rm(outputRoot, { recursive: true, force: true });
await mkdir(packageDirectory, { recursive: true });
for (const name of releaseFiles) {
  const packagedFile = join(packageDirectory, name);
  await cp(join(root, name), packagedFile);
  await utimes(packagedFile, reproducibleTimestamp, reproducibleTimestamp);
}

execFileSync(
  "/usr/bin/zip",
  ["-X", "-q", archive, ...releaseFiles.map((name) => `${manifest.id}/${name}`)],
  { cwd: outputRoot }
);

const archiveEntries = execFileSync("/usr/bin/unzip", ["-Z1", archive], {
  encoding: "utf8"
})
  .trim()
  .split("\n")
  .sort();
const expectedEntries = releaseFiles.map((name) => `${manifest.id}/${name}`).sort();
if (JSON.stringify(archiveEntries) !== JSON.stringify(expectedEntries)) {
  throw new Error(`Unexpected Alpha package entries: ${archiveEntries.join(", ")}`);
}

const releaseHashes = {};
for (const name of releaseFiles) {
  releaseHashes[name] = await sha256(join(packageDirectory, name));
}
const archiveHash = await sha256(archive);
const installerTemplate = await readFile(
  join(root, "scripts", "install-alpha.zsh.in"),
  "utf8"
);
const installerContents = expandInstallerTemplate(installerTemplate, {
  VERSION: manifest.version,
  ARCHIVE_NAME: archiveName,
  ARCHIVE_SHA256: archiveHash,
  MAIN_SHA256: releaseHashes["main.js"]
});
await writeFile(installer, installerContents, "utf8");
await chmod(installer, 0o755);
execFileSync("/bin/zsh", ["-n", installer]);

const installerHash = await sha256(installer);
const releaseManifest = {
  schema_version: 1,
  plugin: {
    id: manifest.id,
    version: manifest.version,
    min_app_version: manifest.minAppVersion,
    desktop_only: manifest.isDesktopOnly === true
  },
  archive: {
    filename: archiveName,
    sha256: archiveHash,
    size_bytes: (await stat(archive)).size,
    entries: expectedEntries
  },
  installer: {
    filename: installerName,
    sha256: installerHash,
    size_bytes: (await stat(installer)).size
  },
  files: Object.fromEntries(
    await Promise.all(
      releaseFiles.map(async (name) => [
        name,
        {
          sha256: releaseHashes[name],
          size_bytes: (await stat(join(packageDirectory, name))).size
        }
      ])
    )
  )
};
await writeFile(
  releaseManifestPath,
  `${JSON.stringify(releaseManifest, null, 2)}\n`,
  "utf8"
);
const releaseManifestHash = await sha256(releaseManifestPath);
await writeFile(
  checksumFile,
  `${archiveHash}  ${archiveName}\n${installerHash}  ${installerName}\n${releaseManifestHash}  ${releaseManifestName}\n`,
  "utf8"
);

console.log(
  JSON.stringify({
    archive,
    checksumFile,
    installer,
    releaseManifest: releaseManifestPath,
    archiveSha256: archiveHash,
    installerSha256: installerHash,
    releaseManifestSha256: releaseManifestHash,
    mainSha256: releaseHashes["main.js"],
    files: releaseFiles,
    version: manifest.version
  })
);

async function sha256(path) {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}
