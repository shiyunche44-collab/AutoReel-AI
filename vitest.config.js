import { defineConfig } from 'vitest/config';

// 前端零构建、零运行时依赖,这套工具链只用来检查:单测 + 类型 + 规范。
export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/web/**/*.test.js'],
    restoreMocks: true,
  },
});
