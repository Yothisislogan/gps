import { defineConfig, devices } from '@playwright/test';
export default defineConfig({
  testDir: './tests/browser', testMatch: '*.spec.mjs', timeout: 30000,
  fullyParallel: true, retries: process.env.CI ? 1 : 0,
  use: { baseURL: 'http://127.0.0.1:8765', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: { command: 'python3 tests/browser/server.py', url: 'http://127.0.0.1:8765', reuseExistingServer: !process.env.CI },
  projects: [
    { name: 'android', use: { ...devices['Pixel 5'], browserName: 'chromium', launchOptions: { args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-webgl'] } } },
    { name: 'iphone', use: { ...devices['iPhone 13'], browserName: 'webkit' } },
  ],
});
