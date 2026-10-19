import { resolve } from 'node:path';
import { defineConfig } from 'vite';

// Multi-page build: one page per prototype, so each bundle can be measured on its own.
export default defineConfig({
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    rollupOptions: {
      input: {
        index: resolve(__dirname, 'index.html'),
        rig2: resolve(__dirname, 'rig2/index.html'),
        relief: resolve(__dirname, 'relief/index.html'),
        threed: resolve(__dirname, 'threed/index.html'),
      },
    },
  },
  server: { port: 5199, strictPort: true },
  preview: { port: 5198, strictPort: true },
});
