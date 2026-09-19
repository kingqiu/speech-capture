import { describe, expect, it } from "vitest";

import {
  writeNodeRequestBody,
  type NodeWritableRequest
} from "../src/node-upload-writer";

describe("writeNodeRequestBody", () => {
  it("waits for drain before writing the remainder of a large upload part", () => {
    const request = new BackpressureRequest();
    const progress: number[] = [];
    const body = new Uint8Array(600 * 1024);

    writeNodeRequestBody(request, body, (uploadedBytes) => {
      progress.push(uploadedBytes);
    });

    expect(request.writeSizes).toEqual([256 * 1024]);
    expect(request.ended).toBe(false);

    request.releaseBackpressure();

    expect(request.writeSizes).toEqual([256 * 1024, 256 * 1024, 88 * 1024]);
    expect(progress).toEqual([256 * 1024, 512 * 1024, 600 * 1024]);
    expect(request.ended).toBe(true);
  });
});

class BackpressureRequest implements NodeWritableRequest {
  public readonly writeSizes: number[] = [];
  public ended = false;
  private drainListener: (() => void) | null = null;

  public once(_event: "drain", listener: () => void): void {
    this.drainListener = listener;
  }

  public write(body: Uint8Array, callback?: () => void): boolean {
    this.writeSizes.push(body.byteLength);
    callback?.();
    return this.writeSizes.length !== 1;
  }

  public end(): void {
    this.ended = true;
  }

  public releaseBackpressure(): void {
    const listener = this.drainListener;
    this.drainListener = null;
    listener?.();
  }
}
