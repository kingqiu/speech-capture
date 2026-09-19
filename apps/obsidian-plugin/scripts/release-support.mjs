export function assertReleaseMetadata(manifest, packageJson, versions) {
  if (
    manifest?.id !== "speech-capture" ||
    typeof manifest.version !== "string" ||
    manifest.version.length === 0 ||
    typeof manifest.minAppVersion !== "string"
  ) {
    throw new Error("The Obsidian manifest is not a valid Speech Capture release.");
  }
  if (packageJson?.version !== manifest.version) {
    throw new Error(
      `Version mismatch: manifest=${manifest.version}, package=${packageJson?.version ?? "missing"}.`
    );
  }
  if (versions?.[manifest.version] !== manifest.minAppVersion) {
    throw new Error(
      `versions.json must map ${manifest.version} to ${manifest.minAppVersion}.`
    );
  }
}

export function expandInstallerTemplate(template, replacements) {
  let rendered = template;
  for (const [name, value] of Object.entries(replacements)) {
    const token = `@@${name}@@`;
    if (!rendered.includes(token)) {
      throw new Error(`Installer template is missing ${token}.`);
    }
    rendered = rendered.replaceAll(token, value);
  }
  const unresolved = rendered.match(/@@[A-Z0-9_]+@@/g);
  if (unresolved !== null) {
    throw new Error(`Unresolved installer tokens: ${unresolved.join(", ")}`);
  }
  return rendered;
}
