import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "../api";
import DriveStudioPage from "./DriveStudioPage";

// ---- mocks ----------------------------------------------------------------
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return {
    ...actual,
    api: {
      ...actual.api,
      driveStatus: vi.fn(),
      driveAuthStart: vi.fn(),
      driveDisconnect: vi.fn(),
      driveRoots: vi.fn(),
      driveFolder: vi.fn(),
      driveScan: vi.fn(),
    },
  };
});

const mocked = vi.mocked(api);

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <DriveStudioPage />
    </QueryClientProvider>
  );
}

// Flush pending promises/timers so React Query resolves under fake timers.
async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("DriveStudioPage connect polling", () => {
  it("opens Google authorization on connect click", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    mocked.driveAuthStart.mockResolvedValue({ auth_url: "https://accounts.google.com/x" });
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);

    renderPage();
    await flush();
    expect(screen.getByText("Google Drive is not connected.")).toBeInTheDocument();

    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    expect(openSpy).toHaveBeenCalledWith(
      "https://accounts.google.com/x",
      "_blank",
      "noopener,noreferrer"
    );
    openSpy.mockRestore();
  });

  it("stops polling and refreshes once Drive is connected", async () => {
    // start disconnected, then become connected after the auth flow
    let connected = false;
    mocked.driveStatus.mockImplementation(() =>
      Promise.resolve({ connected, account_email: "a@b.com", status: "connected" })
    );
    mocked.driveAuthStart.mockResolvedValue({ auth_url: "https://accounts.google.com/x" });
    mocked.driveRoots.mockResolvedValue([]);
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);

    renderPage();
    await flush();
    expect(screen.getByText("Google Drive is not connected.")).toBeInTheDocument();

    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });

    // user completes auth -> now connected
    connected = true;
    await flush(2000);

    expect(screen.getByText(/Connected as a@b\.com/)).toBeInTheDocument();

    // no further status polling runs after success
    const callsAfterConnect = mocked.driveStatus.mock.calls.length;
    await flush(20_000);
    expect(mocked.driveStatus.mock.calls.length).toBe(callsAfterConnect);

    openSpy.mockRestore();
  });

  it("surfaces a timeout error if auth never completes", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    mocked.driveAuthStart.mockResolvedValue({ auth_url: "https://accounts.google.com/x" });
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);

    renderPage();
    await flush();
    expect(screen.getByText("Google Drive is not connected.")).toBeInTheDocument();

    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    // let the mutation resolve and start the polling timer
    await flush();

    // advance beyond the 120s timeout
    await flush(122_000);

    expect(screen.getByText(/did not complete in time/)).toBeInTheDocument();

    openSpy.mockRestore();
  });
});
