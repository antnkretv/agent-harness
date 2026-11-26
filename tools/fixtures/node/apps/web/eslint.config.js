import { layerBoundaries } from "../../.config/eslint-boundaries.js";

import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

export const required = {
  "@typescript-eslint/no-floating-promises": ["error", { ignoreVoid: false, checkThenables: true }],
  "@typescript-eslint/no-misused-promises": "error",
};

export default defineConfig(
  {
    extends: [...tseslint.configs.recommended],
    files: ["src/**/*.{ts,tsx,mts,cts}"],
    languageOptions: { parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname } },
    rules: { ...required },
  },
  ...layerBoundaries({
    source: "src",
    layers: [
      { name: "domain", forbidden: ["application", "ui"] },
      { name: "application", forbidden: ["ui"] },
    ],
  }),
);
