import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  chmod,
  cp,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rm,
  writeFile
} from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const helperSource = join(root, "scripts", "client-update-helper.zsh");
const manifest = JSON.parse(await readFile(join(root, "manifest.json"), "utf8"));
const archiveSource = join(
  root,
  "dist",
  `speech-capture-${manifest.version}-alpha.zip`
);
const release = JSON.parse(
  await readFile(
    join(root, "dist", `speech-capture-${manifest.version}-release.json`),
    "utf8"
  )
);
const temporaryRoot = await mkdtemp("/private/tmp/speech-capture-helper-test.");

try {
  const success = await makeTransaction("success", "0.1.25");
  const successOutput = runHelper(success);
  const successStatus = await readJson(join(success.transactionRoot, "status.json"));
  const installed = await readJson(join(success.plugin, "manifest.json"));
  assert.equal(successStatus.state, "restart_required");
  assert.equal(successStatus.rolled_back, false);
  assert.equal(installed.version, manifest.version);
  assert.equal(await readFile(join(success.plugin, "data.json"), "utf8"), success.data);
  assert.deepEqual(await readdir(success.plugins), ["speech-capture"]);
  const successBackups = await readdir(join(success.config, "plugin-backups"));
  assert.equal(successBackups.some((name) => name.startsWith("speech-capture-")), true);
  assert.equal(
    successBackups.some((name) => name.startsWith("speech-capture.old.duplicate-")),
    true
  );
  assert.match(successOutput, new RegExp(`Speech Capture ${manifest.version} 已安装`));
  await assert.rejects(readFile(join(success.transactionRoot, "request.json")), /ENOENT/);

  const mismatch = await makeTransaction("mismatch", "0.1.24", {
    requestFromVersion: "0.1.25"
  });
  assert.throws(() => runHelper(mismatch), /Command failed/);
  assert.equal((await readJson(join(mismatch.plugin, "manifest.json"))).version, "0.1.24");
  assert.equal((await readJson(join(mismatch.transactionRoot, "status.json"))).error_code, "ACTIVE_PLUGIN_CHANGED");

  const rollback = await makeTransaction("rollback", "0.1.25");
  assert.throws(
    () => runHelper(rollback, { SPEECH_CAPTURE_HELPER_FAIL_AFTER_BACKUP: "1" }),
    /Command failed/
  );
  const rollbackStatus = await readJson(join(rollback.transactionRoot, "status.json"));
  assert.equal(rollbackStatus.error_code, "SYNTHETIC_AFTER_BACKUP_FAILURE");
  assert.equal(rollbackStatus.rolled_back, true);
  assert.equal((await readJson(join(rollback.plugin, "manifest.json"))).version, "0.1.25");
  assert.equal(await readFile(join(rollback.plugin, "data.json"), "utf8"), rollback.data);
  assert.equal((await readdir(rollback.plugins)).includes("speech-capture.old"), true);

  const duplicateRollback = await makeTransaction("duplicate-rollback", "0.1.25");
  assert.throws(
    () =>
      runHelper(duplicateRollback, {
        SPEECH_CAPTURE_HELPER_FAIL_AFTER_DUPLICATES: "1"
      }),
    /Command failed/
  );
  assert.equal(
    (await readJson(join(duplicateRollback.plugin, "manifest.json"))).version,
    "0.1.25"
  );
  assert.equal(
    (await readdir(duplicateRollback.plugins)).includes("speech-capture.old"),
    true
  );
  assert.equal(
    (await readJson(join(duplicateRollback.transactionRoot, "status.json"))).error_code,
    "SYNTHETIC_AFTER_DUPLICATES_FAILURE"
  );

  const corruptArchive = await makeTransaction("corrupt-archive", "0.1.25");
  await writeFile(
    join(corruptArchive.transactionRoot, `speech-capture-${manifest.version}-alpha.zip`),
    "corrupt",
    { flag: "a" }
  );
  assert.throws(() => runHelper(corruptArchive), /Command failed/);
  assert.equal(
    (await readJson(join(corruptArchive.transactionRoot, "status.json"))).error_code,
    "ARCHIVE_HASH_MISMATCH"
  );
  assert.equal(
    (await readJson(join(corruptArchive.plugin, "manifest.json"))).version,
    "0.1.25"
  );

  const noSpace = await makeTransaction("no-space", "0.1.25");
  assert.throws(
    () => runHelper(noSpace, { SPEECH_CAPTURE_HELPER_FORCE_NO_SPACE: "1" }),
    /Command failed/
  );
  assert.equal(
    (await readJson(join(noSpace.transactionRoot, "status.json"))).error_code,
    "INSUFFICIENT_SPACE"
  );
  assert.equal((await readJson(join(noSpace.plugin, "manifest.json"))).version, "0.1.25");

  const customConfig = await makeTransaction("custom-config", "0.1.25", {
    configName: ".obsidian-private"
  });
  runHelper(customConfig);
  assert.equal(
    (await readJson(join(customConfig.plugin, "manifest.json"))).version,
    manifest.version
  );

  const wrongScope = await makeTransaction("wrong-scope", "0.1.25", {
    vaultScopeSha256: "f".repeat(64)
  });
  assert.throws(() => runHelper(wrongScope), /Command failed/);
  assert.equal(
    (await readJson(join(wrongScope.transactionRoot, "status.json"))).error_code,
    "VAULT_SCOPE_MISMATCH"
  );
  assert.equal((await readJson(join(wrongScope.plugin, "manifest.json"))).version, "0.1.25");

  console.log(
    JSON.stringify({
      activePluginChangeRejected: true,
      dataPreserved: true,
      duplicateRestoredOnFailure: true,
      activePluginPreservedBeforeBackup: true,
      corruptArchiveRejectedBeforeMutation: true,
      customConfigDirectoryInstalled: true,
      failedReplacementRolledBack: true,
      helperRequestRemovedAfterCompletion: true,
      installedVersion: installed.version,
      insufficientSpaceRejectedBeforeMutation: true,
      mismatchedVaultScopeRejected: true,
      restartRequiredRecorded: true
    })
  );
} finally {
  await rm(temporaryRoot, { recursive: true, force: true });
}

async function makeTransaction(
  label,
  installedVersion,
  {
    requestFromVersion = installedVersion,
    configName = ".obsidian",
    vaultScopeSha256 = null
  } = {}
) {
  const vault = join(temporaryRoot, label, "vault");
  const config = join(vault, configName);
  const plugins = join(config, "plugins");
  const plugin = join(plugins, "speech-capture");
  const duplicate = join(plugins, "speech-capture.old");
  const transactionId = `update_${createHash("md5").update(label).digest("hex")}`;
  const transactionRoot = join(temporaryRoot, label, transactionId);
  const archive = join(
    transactionRoot,
    `speech-capture-${manifest.version}-alpha.zip`
  );
  const helper = join(transactionRoot, "helper.zsh");
  const currentMain = `current-${label}`;
  const data = `{"worker":"synthetic-${label}"}\n`;

  await mkdir(plugin, { recursive: true });
  await mkdir(duplicate, { recursive: true });
  await mkdir(transactionRoot, { recursive: true, mode: 0o700 });
  await writeFile(
    join(plugin, "manifest.json"),
    JSON.stringify({ id: "speech-capture", version: installedVersion }),
    "utf8"
  );
  await writeFile(join(plugin, "main.js"), currentMain, "utf8");
  await writeFile(join(plugin, "styles.css"), "old", "utf8");
  await writeFile(join(plugin, "data.json"), data, "utf8");
  await writeFile(
    join(duplicate, "manifest.json"),
    JSON.stringify({ id: "speech-capture", version: "0.0.1" }),
    "utf8"
  );
  await cp(archiveSource, archive);
  await cp(helperSource, helper);
  await chmod(helper, 0o700);
  await writeFile(
    join(transactionRoot, "request.json"),
    `${JSON.stringify({
      schema_version: 1,
      transaction_id: transactionId,
      plugin_id: "speech-capture",
      from_version: requestFromVersion,
      to_version: manifest.version,
      min_app_version: manifest.minAppVersion,
      vault_path: vault,
      config_dir_name: configName,
      vault_scope_sha256:
        vaultScopeSha256 ??
        digest(new TextEncoder().encode(`${vault}\0${configName}`)),
      archive_path: archive,
      archive_sha256: release.archive.sha256,
      main_sha256: release.files["main.js"].sha256,
      current_main_sha256: digest(new TextEncoder().encode(currentMain)),
      reopen: false
    }, null, 2)}\n`,
    { encoding: "utf8", mode: 0o600 }
  );
  return { config, data, helper, plugin, plugins, transactionRoot };
}

function runHelper(transaction, extraEnvironment = {}) {
  return execFileSync(
    "/bin/zsh",
    [transaction.helper, join(transaction.transactionRoot, "request.json")],
    {
      encoding: "utf8",
      env: {
        ...process.env,
        SPEECH_CAPTURE_HELPER_SMOKE_TEST: "1",
        ...extraEnvironment
      },
      stdio: ["ignore", "pipe", "pipe"]
    }
  );
}

async function readJson(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

function digest(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}
