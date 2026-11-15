import { describe, it, expect, beforeEach } from "vitest";
import { fmtBytes, fmtDuration, fmtDate, setToken, clearToken, getToken } from "./api";

describe("formatting helpers", () => {
  it("formats bytes", () => {
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(1023)).toBe("1023 B");
    expect(fmtBytes(1024)).toBe("1.0 KB");
    expect(fmtBytes(1048576)).toBe("1.0 MB");
    expect(fmtBytes(5 * 1024 ** 3)).toBe("5.0 GB");
  });

  it("formats durations as m:ss", () => {
    expect(fmtDuration(0)).toBe("0:00");
    expect(fmtDuration(59)).toBe("0:59");
    expect(fmtDuration(60)).toBe("1:00");
    expect(fmtDuration(125)).toBe("2:05");
  });

  it("formats dates and handles null", () => {
    expect(fmtDate(null)).toBe("—");
    expect(typeof fmtDate("2025-01-02T03:04:05")).toBe("string");
  });
});

describe("token storage", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("stores, retrieves and clears the token", () => {
    expect(getToken()).toBeNull();
    setToken("abc123");
    expect(getToken()).toBe("abc123");
    clearToken();
    expect(getToken()).toBeNull();
  });
});
