import { describe, expect, it } from "vitest";

import {
  estimateJobDiskBytes,
  formatBytes,
  formatDurationSeconds,
  isSupportedAudioFile,
  mediaTypeLabel,
  recordingDateHint,
  suggestRecordingDate
} from "../src/intake-draft";

describe("intake draft helpers", () => {
  it("accepts audio MIME types and known extensions without reading file bytes", () => {
    expect(isSupportedAudioFile({ name: "recording.bin", type: "audio/wav" })).toBe(
      true
    );
    expect(isSupportedAudioFile({ name: "recording.M4A", type: "" })).toBe(true);
    expect(isSupportedAudioFile({ name: "notes.md", type: "text/markdown" })).toBe(
      false
    );
  });

  it("uses a valid filename date before file metadata", () => {
    expect(
      suggestRecordingDate(
        "2026-08-03_产品访谈.wav",
        Date.UTC(2026, 6, 20),
        new Date("2026-08-03T12:00:00Z")
      )
    ).toEqual({ value: "2026-08-03", source: "filename" });
    expect(
      suggestRecordingDate(
        "访谈_20260803.wav",
        Date.UTC(2026, 6, 20),
        new Date("2026-08-03T12:00:00Z")
      )
    ).toEqual({ value: "2026-08-03", source: "filename" });
  });

  it("rejects impossible filename dates and falls back to metadata", () => {
    const modified = new Date(2026, 6, 20, 10, 0, 0);
    expect(
      suggestRecordingDate(
        "2026-02-31_访谈.wav",
        modified.getTime(),
        new Date(2026, 7, 3, 12, 0, 0)
      )
    ).toEqual({ value: "2026-07-20", source: "modified" });
  });

  it("provides explicit suggestion provenance", () => {
    expect(recordingDateHint("filename")).toBe("根据文件名建议，可以修改");
    expect(recordingDateHint("modified")).toBe(
      "根据文件修改日期建议，可以修改"
    );
    expect(recordingDateHint("today")).toBe("暂按今天填写，可以修改");
  });

  it("formats source facts without fake precision", () => {
    expect(formatBytes(486 * 1024 * 1024)).toBe("486 MB");
    expect(formatBytes(Math.round(1.2 * 1024 * 1024))).toBe("1.2 MB");
    expect(mediaTypeLabel({ name: "meeting.wav", type: "audio/wav" })).toBe(
      "WAV"
    );
    expect(formatDurationSeconds(42 * 60 + 18)).toBe("42分18秒");
    expect(formatDurationSeconds(3_661)).toBe("1小时1分1秒");
    expect(formatDurationSeconds(null)).toBe("读取中");
    expect(formatBytes(1.7 * 1024 * 1024 * 1024)).toBe("1.7 GB");
    expect(
      estimateJobDiskBytes(486 * 1024 * 1024, 42 * 60 + 18)
    ).toEqual({
      uploadPeakBytes: 1_019_215_872,
      workingBytes: 512_083_456,
      totalBytes: 1_021_691_392
    });
  });
});
