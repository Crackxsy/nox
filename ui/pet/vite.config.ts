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
    globals: false,
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx', '../shared/**/*.test.ts'],
  },
});
