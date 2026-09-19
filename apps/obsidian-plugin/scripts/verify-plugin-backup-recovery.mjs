import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  symlink,
  writeFile
} from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const recovery = join(root, "dist", "recover-speech-capture.zsh");
const temporaryRoot = await mkdtemp("/private/tmp/speech-capture-recovery-test.");

try {
  const success = await makeVault("success");
  const output = runRecovery(success);
  assert.equal((await readManifest(success.plugin)).version, "0.1.27");
  assert.equal(await readFile(join(success.plugin, "data.json"), "utf8"), success.oldData);
  const successBackups = await readdir(success.backupRoot);
  assert.equal(successBackups.includes(success.backupName), false);
  assert.equal(
    successBackups.some((name) => name.startsWith("speech-capture.failed-")),
    true
  );
  assert.match(output, /Speech Capture 已恢复为 0\.1\.27/);

  const interrupted = await makeVault("interrupted");
  assert.throws(
    () =>
      runRecovery(interrupted, {
        SPEECH_CAPTURE_RECOVERY_FAIL_AFTER_ACTIVE: "1"
      }),
    /Command failed/
  );
  assert.equal((await readManifest(interrupted.plugin)).version, "0.1.28");
  assert.equal(
    (await readManifest(join(interrupted.backupRoot, interrupted.backupName))).version,
    "0.1.27"
  );

  const traversal = await makeVault("traversal");
  assert.throws(
    () =>
      execFileSync("/bin/zsh", [recovery, traversal.vault, "../speech-capture-old"], {
        encoding: "utf8",
        env: { ...process.env, SPEECH_CAPTURE_RECOVERY_SMOKE_TEST: "1" },
        stdio: ["ignore", "pipe", "pipe"]
      }),
    /Command failed/
  );
  assert.equal((await readManifest(traversal.plugin)).version, "0.1.28");

  const unsafeData = await makeVault("unsafe-data");
  const unsafeDataPath = join(
    unsafeData.backupRoot,
    unsafeData.backupName,
    "data.json"
  );
  await rm(unsafeDataPath, { force: true });
  await symlink("/private/tmp/does-not-belong-to-the-vault", unsafeDataPath);
  assert.throws(() => runRecovery(unsafeData), /Command failed/);
  assert.equal((await readManifest(unsafeData.plugin)).version, "0.1.28");

  console.log(
    JSON.stringify({
      activeFailurePreserved: true,
      explicitBackupRequired: true,
      interruptedRecoveryRolledBack: true,
      pathTraversalRejected: true,
      restoredDataPreserved: true,
      restoredVersion: "0.1.27",
      unsafeBackupDataRejected: true
    })
  );
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}

async function makeVault(label) {
  const vault = join(temporaryRoot, label, "vault");
  const plugin = join(vault, ".obsidian", "plugins", "speech-capture");
  const backupRoot = join(vault, ".obsidian", "plugin-backups");
  const backupName = "speech-capture-20260919-120000-1234";
  const backup = join(backupRoot, backupName);
  const oldData = `{"worker":"old-${label}"}\n`;
  await mkdir(plugin, { recursive: true });
  await mkdir(backup, { recursive: true });
  await writePlugin(plugin, "0.1.28", `{"worker":"new-${label}"}\n`);
  await writePlugin(backup, "0.1.27", oldData);
  return { backupName, backupRoot, oldData, plugin, vault };
}

async function writePlugin(path, version, data) {
  await writeFile(
    join(path, "manifest.json"),
    JSON.stringify({ id: "speech-capture", version }),
    "utf8"
  );
  await writeFile(join(path, "main.js"), `main-${version}`, "utf8");
  await writeFile(join(path, "styles.css"), `styles-${version}`, "utf8");
  await writeFile(join(path, "data.json"), data, "utf8");
}

function runRecovery(vault, extraEnvironment = {}) {
  return execFileSync("/bin/zsh", [recovery, vault.vault, vault.backupName], {
    encoding: "utf8",
    env: {
      ...process.env,
      SPEECH_CAPTURE_RECOVERY_SMOKE_TEST: "1",
      ...extraEnvironment
    },
    stdio: ["ignore", "pipe", "pipe"]
  });
}

async function readManifest(path) {
  return JSON.parse(await readFile(join(path, "manifest.json"), "utf8"));
}
