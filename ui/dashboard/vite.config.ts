/// <reference types="vitest" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Served by the core HTTP server at /dashboard (IPC Model). Shared code lives one level up.
//
// Tests run in `jsdom` and include `.tsx`: roughly 2,300 lines of component code — every page, the
// shell, the kill switch, the PIN field — had no way of being tested before, which is how a
// one-click kill switch shipped.
export default defineConfig({
  base: '/dashboard/',
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
    setupFiles: ['src/__tests__/setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx', '../shared/**/*.test.ts'],
  },
});
