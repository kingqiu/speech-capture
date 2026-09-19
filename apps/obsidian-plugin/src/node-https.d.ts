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

declare module "node:child_process" {
  export const spawn: unknown;
}

declare module "node:fs/promises" {
  export function chmod(path: string, mode: number): Promise<void>;
  export function lstat(path: string): Promise<{
    isDirectory(): boolean;
    isFile(): boolean;
    isSymbolicLink(): boolean;
    readonly mtimeMs: number;
    readonly size: number;
  }>;
  export function mkdir(
    path: string,
    options?: { readonly recursive?: boolean; readonly mode?: number }
  ): Promise<string | undefined>;
  export function readFile(path: string): Promise<Uint8Array>;
  export function readFile(path: string, encoding: "utf8"): Promise<string>;
  export function readdir(path: string): Promise<string[]>;
  export function rename(oldPath: string, newPath: string): Promise<void>;
  export function utimes(
    path: string,
    atime: Date | number | string,
    mtime: Date | number | string
  ): Promise<void>;
  export function rm(
    path: string,
    options: { readonly recursive?: boolean; readonly force: boolean }
  ): Promise<void>;
  export function writeFile(
    path: string,
    data: string | Uint8Array,
    options?: {
      readonly encoding?: "utf8";
      readonly flag?: "wx";
      readonly mode?: number;
    }
  ): Promise<void>;
}

declare module "node:os" {
  export function homedir(): string;
}

declare module "node:path" {
  export function dirname(path: string): string;
  export function join(...paths: string[]): string;
}

declare module "*.zsh" {
  const contents: string;
  export default contents;
}
