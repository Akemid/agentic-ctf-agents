import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { resolveAdapterDir } from '../lib/paths';

const ROOT = path.resolve(__dirname, '..');

describe('resolveAdapterDir', () => {
  test('finds the adapter from the TypeScript sources (ts-jest)', () => {
    expect(resolveAdapterDir(path.join(ROOT, 'lib'))).toBe(path.join(ROOT, 'adapter'));
  });

  test('finds the adapter from the compiled output (dist/lib), which is where cdk runs', () => {
    expect(resolveAdapterDir(path.join(ROOT, 'dist', 'lib'))).toBe(path.join(ROOT, 'adapter'));
  });

  test('fails with a clear message when there is no adapter next to the app', () => {
    const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'no-adapter-'));
    expect(() => resolveAdapterDir(path.join(empty, 'lib'))).toThrow(/adapter/);
  });
});
