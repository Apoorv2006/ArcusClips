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

/** A fake popup window returned by window.open. */
function makeFakePopup() {
  const popup: {
    location: { replace: ReturnType<typeof vi.fn> };
    close: ReturnType<typeof vi.fn>;
    closed: boolean;
  } = {
    location: { replace: vi.fn() },
    close: vi.fn(function (this: { closed: boolean }) { this.closed = true; }),
    closed: false,
  };
  return popup;
}

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

describe("DriveStudioPage connect popup", () => {
  it("opens a blank popup synchronously, then navigates it to the auth URL", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    mocked.driveAuthStart.mockResolvedValue({ auth_url: "https://accounts.google.com/x" });
    const popup = makeFakePopup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    renderPage();
    await flush();
    expect(screen.getByText("Google Drive is not connected.")).toBeInTheDocument();

    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    // blank popup opened synchronously (empty URL first arg)
    expect(openSpy).toHaveBeenCalledWith("", "_blank", "noopener,noreferrer");
    // then the already-open popup is navigated to the auth URL
    expect(popup.location.replace).toHaveBeenCalledWith("https://accounts.google.com/x");
    openSpy.mockRestore();
  });

  it("surfaces a visible error when the popup is blocked", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

    renderPage();
    await flush();
    expect(screen.getByText("Google Drive is not connected.")).toBeInTheDocument();

    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    expect(screen.getByText(/Popup was blocked/)).toBeInTheDocument();
    openSpy.mockRestore();
  });

  it("stops polling, closes the popup and refreshes once connected", async () => {
    let connected = false;
    mocked.driveStatus.mockImplementation(() =>
      Promise.resolve({
        connected,
        account_email: "a@b.com",
        status: connected ? "connected" : "disconnected",
      })
    );
    mocked.driveAuthStart.mockResolvedValue({ auth_url: "https://accounts.google.com/x" });
    mocked.driveRoots.mockResolvedValue([]);
    const popup = makeFakePopup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    renderPage();
    await flush();
    expect(screen.getByText("Google Drive is not connected.")).toBeInTheDocument();

    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    connected = true;
    await flush(2000);
    await flush(2000);
    await flush(0);

    expect(screen.getByText(/Connected as a@b\.com/)).toBeInTheDocument();
    // the auth popup was closed after success
    expect(popup.close).toHaveBeenCalled();

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
    const popup = makeFakePopup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    renderPage();
    await flush();
    expect(screen.getByText("Google Drive is not connected.")).toBeInTheDocument();

    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    // advance beyond the 120s timeout
    await flush(122_000);

    expect(screen.getByText(/did not complete in time/)).toBeInTheDocument();

    openSpy.mockRestore();
  });
});
