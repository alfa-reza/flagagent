import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/real-docker*.test.ts", "tests/**/integration.*.test.ts"],
    environment: "node",
    testTimeout: 120000,
    hookTimeout: 120000,
  },
});
