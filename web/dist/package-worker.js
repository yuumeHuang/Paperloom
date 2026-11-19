import { unzipSync } from './vendor/fflate.js';
import { inspectZip } from './package-core.js';

self.onmessage = event => {
  try {
    const bytes = new Uint8Array(event.data);
    inspectZip(bytes);
    const files = unzipSync(bytes);
    const buffers = [...new Set(Object.values(files).map(file => file.buffer))];
    self.postMessage({files},buffers);
  } catch {
    self.postMessage({error:true});
  }
};
