/**
 * Lint rules for the dashboard.
 *
 * This file exists because five `// eslint-disable-next-line react-hooks/exhaustive-deps` comments
 * in the source claimed a linter that did not exist — and the bug those comments were suppressing
 * (a memoised callback reading a stale piece of state) was a real one that shipped.
 *
 * Three rule sets, no style bikeshedding: the TypeScript recommended set, `react-hooks` (the one
 * that catches the class of bug above), and `jsx-a11y` (the one that catches a control without a
 * label). Formatting is not linted; it is not worth a CI failure.
 */

import js from '@eslint/js';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', '../shared/generated/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { 'react-hooks': reactHooks, 'jsx-a11y': jsxA11y },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...jsxA11y.flatConfigs.recommended.rules,
      // `_`-prefixed arguments are the documented way to say "required by the signature, unused".
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // The house rule the review checked by hand: no `any` anywhere in ui/**.
      '@typescript-eslint/no-explicit-any': 'error',
      'no-console': ['warn', { allow: ['warn', 'error', 'info'] }],
      eqeqeq: ['error', 'smart'],
      // A scrollable container has to be reachable from the keyboard (WCAG 2.1.1): without a
      // tabindex the provider rail could be scrolled with a mouse and by nothing else. The rule's
      // own escape hatch is naming the roles where that is legitimate.
      'jsx-a11y/no-noninteractive-tabindex': [
        'error',
        { tags: [], roles: ['tabpanel', 'group', 'region'], allowExpressionValues: true },
      ],
    },
  },
  {
    files: ['**/__tests__/**/*.{ts,tsx}'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
);
