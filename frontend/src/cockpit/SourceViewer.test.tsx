import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SourceViewer, type CitationSource } from "./SourceViewer";

function source(over: Partial<CitationSource> = {}): CitationSource {
  return {
    evidence_id: "ev-1",
    reference_id: "r1",
    source_line: "smith2021 · The Smith 2021 Study",
    claim: "The claim sentence.",
    stance: "supports",
    confidence: null,
    index: 1,
    total: 2,
    prev_ev: null,
    next_ev: "ev-2",
    tombstoned: false,
    no_text: false,
    note: null,
    text: "abcNEEDLEdef",
    highlight: [3, 9],
    ...over,
  };
}

describe("SourceViewer", () => {
  const noop = () => {};

  it("wraps the highlight span in a <mark>", () => {
    const { container } = render(
      <SourceViewer source={source()} pinned={false} onClose={noop} onPin={noop} onNavigate={noop} />,
    );
    const mark = container.querySelector("mark");
    expect(mark?.textContent).toBe("NEEDLE");
  });

  it("renders the source line, claim and position", () => {
    render(
      <SourceViewer source={source()} pinned={false} onClose={noop} onPin={noop} onNavigate={noop} />,
    );
    expect(screen.getByText(/The Smith 2021 Study/)).toBeInTheDocument();
    expect(screen.getByText(/The claim sentence\./)).toBeInTheDocument();
    expect(screen.getByText(/1 of 2/)).toBeInTheDocument();
  });

  it("shows the not-available notice when no_text", () => {
    render(
      <SourceViewer
        source={source({ no_text: true, text: "", highlight: null })}
        pinned={false}
        onClose={noop}
        onPin={noop}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/isn.t available yet/i)).toBeInTheDocument();
  });

  it("renders a tombstoned banner", () => {
    render(
      <SourceViewer
        source={source({ tombstoned: true })}
        pinned={false}
        onClose={noop}
        onPin={noop}
        onNavigate={noop}
      />,
    );
    expect(screen.getByText(/has been removed/i)).toBeInTheDocument();
  });

  it("fires onNavigate with next_ev and disables prev when prev_ev is null", () => {
    const onNavigate = vi.fn();
    render(
      <SourceViewer source={source()} pinned={false} onClose={noop} onPin={noop} onNavigate={onNavigate} />,
    );
    expect(screen.getByRole("button", { name: /previous citation/i })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /next citation/i }));
    expect(onNavigate).toHaveBeenCalledWith("ev-2");
  });

  it("shows Pin when not pinned and hides it when pinned; Close always fires", () => {
    const onPin = vi.fn();
    const onClose = vi.fn();
    const { rerender } = render(
      <SourceViewer source={source()} pinned={false} onClose={onClose} onPin={onPin} onNavigate={noop} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /pin source/i }));
    expect(onPin).toHaveBeenCalledOnce();
    rerender(
      <SourceViewer source={source()} pinned onClose={onClose} onPin={onPin} onNavigate={noop} />,
    );
    expect(screen.queryByRole("button", { name: /pin source/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /close source/i }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
