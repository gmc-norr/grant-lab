import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it } from "vitest";

import { resetEditorStore, setCursor, setSave } from "../cockpit/editorStore";
import { StatusBar } from "./StatusBar";

afterEach(() => resetEditorStore());

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <StatusBar />
    </MemoryRouter>,
  );
}

it("off the cockpit, shows only the global label", () => {
  renderAt("/");
  expect(screen.getByText("Call Manager")).toBeInTheDocument();
  expect(screen.queryByText("Markdown")).not.toBeInTheDocument();
});

it("on the cockpit, shows language + Ln/Col + save state", () => {
  setCursor({ line: 12, col: 4 });
  setSave("dirty");
  renderAt("/cockpit/ws-1");
  expect(screen.getByText("Markdown")).toBeInTheDocument();
  expect(screen.getByText("Ln 12, Col 4")).toBeInTheDocument();
  expect(screen.getByText("Unsaved")).toBeInTheDocument();
});
