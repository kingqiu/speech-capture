const UPLOAD_PROGRESS_SLICE_BYTES = 256 * 1024;

export interface NodeWritableRequest {
  once(event: "drain", listener: () => void): void;
  write(body: Uint8Array, callback?: () => void): boolean;
  end(body?: string | Uint8Array): void;
}

export function writeNodeRequestBody(
  request: NodeWritableRequest,
  body: string | Uint8Array,
  onUploadProgress?: (uploadedBytes: number) => void
): void {
  if (typeof body === "string") {
    request.end(body);
    return;
  }
  let offset = 0;
  const writeAvailable = (): void => {
    while (offset < body.byteLength) {
      const end = Math.min(offset + UPLOAD_PROGRESS_SLICE_BYTES, body.byteLength);
      const canContinue = request.write(
        body.subarray(offset, end),
        onUploadProgress === undefined ? undefined : () => onUploadProgress(end)
      );
      offset = end;
      if (!canContinue) {
        request.once("drain", writeAvailable);
        return;
      }
    }
    request.end();
  };
  writeAvailable();
}
