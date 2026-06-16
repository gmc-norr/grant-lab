// Vitest setup: register jest-dom matchers (toBeDisabled, toBeInTheDocument, …)
// and their `expect` type augmentation. Kept in src/ so tsconfig (include:
// ["src"]) typechecks the augmentation for all *.test.tsx files.
import "@testing-library/jest-dom/vitest";
