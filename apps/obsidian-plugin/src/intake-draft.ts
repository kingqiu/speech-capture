export interface RecordingDateSuggestion {
  readonly value: string;
  readonly source: "filename" | "modified" | "today";
}

const AUDIO_EXTENSIONS = new Set([
  "aac",
  "flac",
  "m4a",
  "mp3",
  "mp4",
  "ogg",
  "opus",
  "wav",
  "webm"
]);

export function isSupportedAudioFile(file: Pick<File, "name" | "type">): boolean {
  if (file.type.startsWith("audio/")) {
    return true;
  }
  const extension = file.name.split(".").pop()?.toLowerCase();
  return extension !== undefined && AUDIO_EXTENSIONS.has(extension);
}

export function suggestRecordingDate(
  fileName: string,
  lastModified: number,
  now: Date = new Date()
): RecordingDateSuggestion {
  const filenameDate = parseFilenameDate(fileName);
  if (filenameDate !== null) {
    return { value: filenameDate, source: "filename" };
  }
  if (Number.isFinite(lastModified) && lastModified > 0) {
    const modified = new Date(lastModified);
    if (!Number.isNaN(modified.getTime()) && modified.getTime() <= now.getTime()) {
      return { value: localDate(modified), source: "modified" };
    }
  }
  return { value: localDate(now), source: "today" };
}

export function recordingDateHint(
  source: RecordingDateSuggestion["source"]
): string {
  switch (source) {
    case "filename":
      return "根据文件名建议，可以修改";
    case "modified":
      return "根据文件修改日期建议，可以修改";
    case "today":
      return "暂按今天填写，可以修改";
  }
}

export async function readAudioDurationSeconds(file: File): Promise<number | null> {
  const sourceUrl = URL.createObjectURL(file);
  const audio = document.createElement("audio");
  audio.preload = "metadata";
  return await new Promise((resolve) => {
    let settled = false;
    const finish = (value: number | null): void => {
      if (settled) {
        return;
      }
      settled = true;
      window.clearTimeout(timeout);
      audio.removeAttribute("src");
      audio.load();
      URL.revokeObjectURL(sourceUrl);
      resolve(value);
    };
    const timeout = window.setTimeout(() => finish(null), 10_000);
    audio.addEventListener(
      "loadedmetadata",
      () =>
        finish(
          Number.isFinite(audio.duration) && audio.duration > 0
            ? audio.duration
            : null
        ),
      { once: true }
    );
    audio.addEventListener("error", () => finish(null), { once: true });
    audio.src = sourceUrl;
  });
}

export function formatDurationSeconds(value: number | null): string {
  if (value === null || !Number.isFinite(value) || value < 0) {
    return "读取中";
  }
  const totalSeconds = Math.round(value);
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  return [
    ...(hours > 0 ? [`${hours.toString()}小时`] : []),
    ...(minutes > 0 || hours > 0 ? [`${minutes.toString()}分`] : []),
    `${seconds.toString()}秒`
  ].join("");
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) {
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  }
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

export function estimateJobDiskBytes(
  sourceSizeBytes: number,
  durationSeconds: number | null
): { readonly workingBytes: number; readonly totalBytes: number } | null {
  if (
    durationSeconds === null ||
    !Number.isFinite(durationSeconds) ||
    durationSeconds <= 0 ||
    !Number.isSafeInteger(sourceSizeBytes) ||
    sourceSizeBytes <= 0
  ) {
    return null;
  }
  const pcmBytes = Math.ceil(durationSeconds * 16_000 * 2);
  const workingAudioBytes = pcmBytes * 3;
  const artifactHeadroomBytes = Math.max(
    256 * 1024 * 1024,
    Math.ceil(sourceSizeBytes * 0.1)
  );
  const workingBytes = workingAudioBytes + artifactHeadroomBytes;
  return { workingBytes, totalBytes: sourceSizeBytes + workingBytes };
}

export function mediaTypeLabel(file: Pick<File, "name" | "type">): string {
  const extension = file.name.split(".").pop()?.toUpperCase();
  return extension || file.type || "AUDIO";
}

function parseFilenameDate(fileName: string): string | null {
  const separated = fileName.match(
    /(?:^|[^0-9])((?:19|20)\d{2})[-_. ](\d{1,2})[-_. ](\d{1,2})(?:[^0-9]|$)/
  );
  const compact = fileName.match(
    /(?:^|[^0-9])((?:19|20)\d{2})(\d{2})(\d{2})(?:[^0-9]|$)/
  );
  const match = separated ?? compact;
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (!isValidDate(year, month, day)) {
    return null;
  }
  return `${year.toString().padStart(4, "0")}-${month
    .toString()
    .padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
}

function isValidDate(year: number, month: number, day: number): boolean {
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

function localDate(date: Date): string {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}
