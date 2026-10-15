import * as fs from 'fs';
import * as path from 'path';

/**
 * The adapter directory (Dockerfile + package) sits next to `lib/`. The app runs from `dist/lib` once
 * compiled and from `lib` under ts-jest, so look one and two levels up.
 */
export function resolveAdapterDir(fromDir: string): string {
  for (const levels of [['..'], ['..', '..']]) {
    const candidate = path.resolve(fromDir, ...levels, 'adapter');
    if (fs.existsSync(path.join(candidate, 'Dockerfile'))) return candidate;
  }
  throw new Error(`Cannot find the adapter directory (with a Dockerfile) next to ${fromDir}`);
}
