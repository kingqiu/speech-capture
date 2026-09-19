declare module "node:https" {
  export const Agent: unknown;
  export const request: unknown;
}

declare module "node:zlib" {
  export function deflateRawSync(data: Uint8Array): Uint8Array;
  export function inflateRawSync(
    data: Uint8Array,
    options?: { readonly maxOutputLength?: number }
  ): Uint8Array;
}
