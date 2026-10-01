/// <reference types="vitest" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Served by the core HTTP server at /pet (IPC Model). Shared code lives one level up.
//
// Same vitest setup as the dashboard, including the shared tests: both apps depend on
// `ui/shared`, so both apps run its tests rather than one package covering the other's risk.
export default defineConfig({
  base: '/pet/',
  plugins: [react()],
  server: { fs: { allow: ['..'] } },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2022',
  },
  test: {
    environment: 'jsdom',
    // 5s is vitest's default and it is tight here: `../shared/__tests__/envelope.test.ts` starts
    // a real WebSocket server, and on a loaded CI runner a worker doing that can starve a plain
    // synchronous test in the same pool past the deadline. That is what happened - a test that
    // only formats a date was reported as timing out. Raising the deadline treats the symptom,
    // and the symptom is the only thing wrong: the tests themselves take milliseconds.
    testTimeout: 20000,
    globals: false,
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx', '../shared/**/*.test.ts'],
  },
});
