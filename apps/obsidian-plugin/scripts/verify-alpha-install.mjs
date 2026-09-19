import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  appendFile,
  cp,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  writeFile
} from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const manifest = JSON.parse(await readFile(join(root, "manifest.json"), "utf8"));
const installer = join(
  root,
  "dist",
  `install-${manifest.id}-${manifest.version}.zsh`
);
const archiveName = `${manifest.id}-${manifest.version}-alpha.zip`;
const archive = join(root, "dist", archiveName);
const temporaryRoot = await mkdtemp("/private/tmp/speech-capture-install-test.");
const vault = join(temporaryRoot, "vault");
const plugins = join(vault, ".obsidian", "plugins");
const current = join(plugins, manifest.id);
const duplicate = join(plugins, `${manifest.id}.backup-old`);
const preservedData = '{"worker":"synthetic-test"}\n';

try {
  await mkdir(current, { recursive: true });
  await mkdir(duplicate, { recursive: true });
  await writeFile(join(current, "data.json"), preservedData, "utf8");
  await writeFile(
    join(current, "manifest.json"),
    JSON.stringify({ id: manifest.id, version: "0.0.1" }),
    "utf8"
  );
  await writeFile(
    join(duplicate, "manifest.json"),
    JSON.stringify({ id: manifest.id, version: "0.0.0" }),
    "utf8"
  );

  const output = execFileSync("/bin/zsh", [installer, vault], {
    encoding: "utf8",
    env: { ...process.env, SPEECH_CAPTURE_INSTALL_SMOKE_TEST: "1" }
  });

  const installedManifest = JSON.parse(
    await readFile(join(current, "manifest.json"), "utf8")
  );
  assert.equal(installedManifest.id, manifest.id);
  assert.equal(installedManifest.version, manifest.version);
  assert.equal(await readFile(join(current, "data.json"), "utf8"), preservedData);
  assert.deepEqual(await readdir(plugins), [manifest.id]);

  const backups = await readdir(join(vault, ".obsidian", "plugin-backups"));
  assert.equal(backups.some((name) => name.startsWith(`${manifest.id}-`)), true);
  assert.equal(
    backups.some((name) => name.startsWith(`${manifest.id}.backup-old.duplicate-`)),
    true
  );
  assert.match(output, new RegExp(`Speech Capture ${manifest.version} 已安装并校验通过`));

  const corruptBundle = join(temporaryRoot, "corrupt-bundle");
  const corruptVault = join(temporaryRoot, "corrupt-vault");
  const corruptPlugin = join(
    corruptVault,
    ".obsidian",
    "plugins",
    manifest.id
  );
  await mkdir(corruptBundle, { recursive: true });
  await mkdir(corruptPlugin, { recursive: true });
  await cp(installer, join(corruptBundle, `install-${manifest.id}-${manifest.version}.zsh`));
  await cp(archive, join(corruptBundle, archiveName));
  await appendFile(join(corruptBundle, archiveName), "corrupt", "utf8");
  await writeFile(
    join(corruptPlugin, "manifest.json"),
    JSON.stringify({ id: manifest.id, version: "0.0.2" }),
    "utf8"
  );
  assert.throws(
    () =>
      execFileSync(
        "/bin/zsh",
        [join(corruptBundle, `install-${manifest.id}-${manifest.version}.zsh`), corruptVault],
        {
          encoding: "utf8",
          env: { ...process.env, SPEECH_CAPTURE_INSTALL_SMOKE_TEST: "1" }
        }
      ),
    /Command failed/
  );
  const untouchedManifest = JSON.parse(
    await readFile(join(corruptPlugin, "manifest.json"), "utf8")
  );
  assert.equal(untouchedManifest.version, "0.0.2");

  console.log(
    JSON.stringify({
      corruptArchiveRejectedBeforeInstall: true,
      dataPreserved: true,
      duplicateMovedOutsidePluginDirectory: true,
      installedVersion: installedManifest.version,
      rollbackBackupCreated: true
    })
  );
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}
