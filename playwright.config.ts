import { defineConfig } from '@playwright/test';
import { existsSync } from 'node:fs';

export default defineConfig({
  testDir: './tests/ui',
  timeout: 30_000,
  fullyParallel: false,
  reporter: process.env.CI ? [['github'], ['list']] : [['list']],
  use: {
    baseURL: 'http://127.0.0.1:1420',
    headless: true,
    launchOptions: { executablePath: process.env.Q22_CHROMIUM || (existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : undefined) },
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://127.0.0.1:1420',
    reuseExistingServer: !process.env.CI,
  },
});
