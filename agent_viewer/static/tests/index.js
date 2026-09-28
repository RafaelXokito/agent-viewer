// Entry point so that `node --test agent_viewer/static/tests/` works as SPEC
// section 13 writes it: Node resolves a directory argument to its index.js,
// which loads every *.test.js file here.

import { readdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const files = readdirSync(here).filter((name) => name.endsWith('.test.js')).sort();
for (const name of files) {
  await import(pathToFileURL(`${here}/${name}`).href);
}
