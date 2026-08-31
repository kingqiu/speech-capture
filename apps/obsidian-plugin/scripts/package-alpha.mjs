import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const manifest = JSON.parse(await readFile(join(root, "manifest.json"), "utf8"));
if (manifest.id !== "speech-capture" || typeof manifest.version !== "string") {
  throw new Error("The Obsidian manifest is not a valid Speech Capture release.");
}

const outputRoot = join(root, "dist");
const packageDirectory = join(outputRoot, manifest.id);
const archive = join(outputRoot, `${manifest.id}-${manifest.version}-alpha.zip`);
const checksumFile = `${archive}.sha256`;
const releaseFiles = ["main.js", "manifest.json", "styles.css"];

await rm(outputRoot, { recursive: true, force: true });
await mkdir(packageDirectory, { recursive: true });
for (const name of releaseFiles) {
  await cp(join(root, name), join(packageDirectory, name));
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

const hashes = [];
for (const name of releaseFiles) {
  hashes.push(`${await sha256(join(packageDirectory, name))}  ${manifest.id}/${name}`);
}
hashes.push(`${await sha256(archive)}  ${archive.split("/").at(-1)}`);
await writeFile(checksumFile, `${hashes.join("\n")}\n`, "utf8");

console.log(
  JSON.stringify({
    archive,
    checksumFile,
    files: releaseFiles,
    version: manifest.version
  })
);

async function sha256(path) {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}
