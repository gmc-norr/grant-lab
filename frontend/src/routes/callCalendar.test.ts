import { describe, expect, it } from "vitest";
import type { CallDetail } from "../api/calls";
import { callCalendar, callShareUrl } from "./callCalendar";

const call = { id: 7, title: "Funding; clinical research", source_name: "Funder", url: "https://example.org/call", deadline: "2026-10-25T17:30:00+01:00", decision_note: "PRIVATE NOTE" } as CallDetail["call"];

describe("deadline export", () => {
  it("uses the exact deadline instant and excludes private notes", () => {
    const calendar = callCalendar(call);
    expect(calendar).toContain("DTSTART:20261025T163000Z");
    expect(calendar).toContain("BEGIN:VCALENDAR");
    expect(calendar).toContain("Funding\\; clinical research");
    expect(calendar).not.toContain("PRIVATE NOTE");
  });
  it("does not invent missing or invalid deadlines", () => {
    expect(callCalendar({ ...call, deadline: null })).toBeNull();
    expect(callCalendar({ ...call, deadline: "invalid" })).toBeNull();
  });
  it("shares only the call and selected profile, not view or private text", () => {
    expect(callShareUrl("https://grants.example.org", 7, 3)).toBe("https://grants.example.org/calls/7?profile=3");
    expect(callShareUrl("https://grants.example.org", 7, null)).toBe("https://grants.example.org/calls/7");
  });
});