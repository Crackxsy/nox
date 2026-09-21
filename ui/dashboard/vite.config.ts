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
    globals: false,
    setupFiles: ['src/__tests__/setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx', '../shared/**/*.test.ts'],
  },
});
