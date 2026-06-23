import { createTheme, type MantineColorsTuple } from "@mantine/core";

// Teal scale, light -> dark. Index 6 = primary accent (#14b8a6); index 5 =
// the brighter accent (#2dd4bf) used as the visible primary on the dark scheme.
const teal: MantineColorsTuple = [
  "#e6fcf7",
  "#c8f6ec",
  "#9aeeda",
  "#66e3c8",
  "#3ed8b8",
  "#2dd4bf",
  "#14b8a6",
  "#0f9d8f",
  "#0f766e",
  "#0c5d57",
];

export const theme = createTheme({
  primaryColor: "teal",
  primaryShade: { light: 6, dark: 5 },
  colors: { teal },
  fontFamily:
    '-apple-system, "Segoe UI", system-ui, "Helvetica Neue", Arial, sans-serif',
  fontFamilyMonospace:
    '"Cascadia Code", "JetBrains Mono", "SF Mono", "Consolas", ui-monospace, monospace',
  defaultRadius: "sm",
  radius: { sm: "4px" },
});
