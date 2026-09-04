import { createTheme, type MantineColorsTuple } from "@mantine/core";

// Teal scale, light -> dark. Index 6 = primary accent (#14b8a6); index 5 =
// the brighter accent (#2dd4bf) used as the visible primary on the dark scheme;
// index 8 (#0f766e) is the primary on the LIGHT scheme the match edition uses.
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
  // `light: 8`, not Mantine's 6. Until the match edition there WAS no light
  // scheme here, so nothing ever exercised the light half of this pair and its
  // value was never an answer to a question anyone asked. Shade 6 (#14b8a6)
  // measured 2.37:1 on the off-white ground as the filled-button and link
  // colour; shade 8 (#0f766e) is 5.20:1, and white on it is 5.48:1. It is the
  // same value theme.match.css already picked for --app-accent / --app-link.
  // Inert for the full edition, which renders dark and has no scheme toggle
  // (verified: no useMantineColorScheme/setColorScheme anywhere in src/), and
  // measured inert — every --mantine-* variable and every rendered colour on
  // the full build's dashboard and profiles pages is unchanged by this line.
  primaryShade: { light: 8, dark: 5 },
  colors: { teal },
  fontFamily:
    '-apple-system, "Segoe UI", system-ui, "Helvetica Neue", Arial, sans-serif',
  fontFamilyMonospace:
    '"Cascadia Code", "JetBrains Mono", "SF Mono", "Consolas", ui-monospace, monospace',
  defaultRadius: "sm",
  radius: { sm: "4px" },
});
