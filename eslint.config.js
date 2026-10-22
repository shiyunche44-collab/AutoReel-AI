import js from '@eslint/js';
import globals from 'globals';

// 前端是零构建的原生 ES Module:浏览器直接跑,不打包、不转译。
// 这套规则只管"会出事"的部分,风格交给 Prettier。
export default [
  {
    ignores: ['node_modules/**', 'motion/**', 'data/**'],
  },
  js.configs.recommended,
  {
    files: ['app/web/static/js/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser },
    },
    rules: {
      // 这几个是这个项目的既有写法,不是错
      'no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      'no-empty': ['error', { allowEmptyCatch: true }],
      // 真会出事的写法
      eqeqeq: ['error', 'smart'],
      'no-var': 'error',
      'prefer-const': 'error',
      'no-implicit-globals': 'error',
      'no-return-await': 'error',
      'require-atomic-updates': 'off',
    },
  },
  {
    files: ['tests/web/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node },
    },
  },
];
