import type { JobSchema } from "../../../packages/protocol/generated/typescript/speech-capture-protocol";

export function sameJobListPresentation(
  previous: readonly JobSchema[],
  next: readonly JobSchema[]
): boolean {
  if (previous.length !== next.length) {
    return false;
  }
  return previous.every((job, index) => {
    const candidate = next[index];
    return (
      candidate !== undefined &&
      job.job_id === candidate.job_id &&
      job.revision === candidate.revision &&
      job.state === candidate.state &&
      job.source_display_name === candidate.source_display_name &&
      job.recording_date === candidate.recording_date &&
      job.updated_at === candidate.updated_at
    );
  });
}
