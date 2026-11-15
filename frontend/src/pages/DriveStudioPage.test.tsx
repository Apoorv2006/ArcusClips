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
    focus: ReturnType<typeof vi.fn>;
    closed: boolean;
    opener: unknown;
  } = {
    location: { replace: vi.fn() },
    close: vi.fn(function (this: { closed: boolean }) { this.closed = true; }),
    focus: vi.fn(),
    closed: false,
    opener: { original: "opener" },
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
  it("opens a blank popup synchronously in the click handler, before the API call", async () => {
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
    // The popup must be opened synchronously inside the click handler,
    // before any await/flush.
    expect(openSpy).toHaveBeenCalledTimes(1);
    await flush();

    // Synchronous, user-initiated open with an empty URL — and crucially
    // WITHOUT "noopener,noreferrer" (which would sever the opener handle).
    expect(openSpy.mock.calls[0]).toEqual(["", "_blank"]);
    openSpy.mockRestore();
  });

  it("navigates the same popup to the auth URL with opener set to null first", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    mocked.driveAuthStart.mockResolvedValue({ auth_url: "https://accounts.google.com/x" });
    const popup = makeFakePopup();
    let openerAtNavigation: unknown = "not-navigated";
    popup.location.replace = vi.fn(() => {
      openerAtNavigation = popup.opener;
    });
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    renderPage();
    await flush();
    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    // The SAME popup (opened synchronously) is navigated, not a new one.
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(popup.location.replace).toHaveBeenCalledWith("https://accounts.google.com/x");
    // opener was cleared BEFORE the navigation happened.
    expect(openerAtNavigation).toBeNull();
    expect(popup.opener).toBeNull();
    openSpy.mockRestore();
  });

  it("does not call the auth API when the popup is blocked", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

    renderPage();
    await flush();
    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    expect(screen.getByText(/Popup was blocked/)).toBeInTheDocument();
    expect(mocked.driveAuthStart).not.toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it("closes the blank popup and surfaces an error when the auth API fails", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    mocked.driveAuthStart.mockRejectedValue(new Error("boom"));
    const popup = makeFakePopup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    renderPage();
    await flush();
    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    // the now-useless blank popup was closed
    expect(popup.close).toHaveBeenCalled();
    expect(popup.location.replace).not.toHaveBeenCalled();
    expect(screen.getByText(/Could not start Google Drive authorization/)).toBeInTheDocument();
    // polling was not started
    const statusCalls = mocked.driveStatus.mock.calls.length;
    await flush(10_000);
    expect(mocked.driveStatus.mock.calls.length).toBe(statusCalls);
    openSpy.mockRestore();
  });

  it("does not navigate a popup the user closed while the auth URL was loading", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    const popup = makeFakePopup();
    mocked.driveAuthStart.mockImplementation(() => {
      popup.closed = true; // user closed the popup mid-request
      return Promise.resolve({ auth_url: "https://accounts.google.com/x" });
    });
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    renderPage();
    await flush();
    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    expect(popup.location.replace).not.toHaveBeenCalled();
    expect(screen.getByText(/closed before authorization could start/)).toBeInTheDocument();
    openSpy.mockRestore();
  });

  it("closes the stranded popup and surfaces an error when navigation fails", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    mocked.driveAuthStart.mockResolvedValue({ auth_url: "https://accounts.google.com/x" });
    const popup = makeFakePopup();
    popup.location.replace = vi.fn(() => {
      throw new Error("denied by browser");
    });
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    renderPage();
    await flush();
    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    expect(popup.close).toHaveBeenCalled();
    expect(screen.getByText(/Could not navigate to Google/)).toBeInTheDocument();
    openSpy.mockRestore();
  });

  it("stops polling and clears the timer when the page unmounts mid-auth", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    mocked.driveAuthStart.mockResolvedValue({ auth_url: "https://accounts.google.com/x" });
    const popup = makeFakePopup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    const { unmount } = renderPage();
    await flush();
    act(() => {
      screen.getByRole("button", { name: "Connect Google Drive" }).click();
    });
    await flush();

    // polling is running (auth not complete)
    await flush(2000);
    const callsBeforeUnmount = mocked.driveStatus.mock.calls.length;
    expect(callsBeforeUnmount).toBeGreaterThan(1);

    unmount();
    await flush(10_000);
    // no more status polling after unmount — the timer was cleared
    expect(mocked.driveStatus.mock.calls.length).toBeLessThanOrEqual(callsBeforeUnmount + 1);
    // the orphaned auth popup was closed on unmount
    expect(popup.close).toHaveBeenCalled();
    openSpy.mockRestore();
  });

  it("focuses the already-open popup instead of orphaning it on a second click", async () => {
    const disconnected = { connected: false, account_email: "", status: "disconnected" };
    mocked.driveStatus.mockResolvedValue(disconnected);
    mocked.driveAuthStart.mockImplementation(
      () => new Promise<{ auth_url: string }>(() => {}) // never resolves
    );
    const popup = makeFakePopup();
    const openSpy = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);

    renderPage();
    await flush();
    const button = screen.getByRole("button", { name: "Connect Google Drive" });
    act(() => {
      button.click();
      button.click();
    });
    await flush();

    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(popup.focus).toHaveBeenCalled();
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

describe("DriveStudioPage Shared Drive scanning", () => {
  it("scans My Drive and Shared Drive selections in separate requests and aggregates", async () => {
    mocked.driveStatus.mockResolvedValue({
      connected: true, account_email: "a@b.com", status: "connected",
    } as never);
    mocked.driveRoots.mockResolvedValue([
      { id: "root", name: "My Drive", kind: "folder", is_root: true, file_count: 0, folder_count: 1 },
      { id: "sd-1", name: "Shared Drive One", kind: "drive", is_root: true, file_count: 0, folder_count: 1 },
    ] as never);
    mocked.driveFolder.mockImplementation((folderId: string, driveId?: string) => {
      if (folderId === "root") {
        // My Drive children must NOT carry a driveId
        expect(driveId).toBeUndefined();
        return Promise.resolve({ folders: [], video_files: [] }) as never;
      }
      if (folderId === "sd-1") {
        // Shared Drive children carry the driveId
        expect(driveId).toBe("sd-1");
        return Promise.resolve({
          folders: [{ id: "sd-sub", name: "Season 1", kind: "folder", is_root: false, file_count: 0, folder_count: 0 }],
          video_files: [],
        }) as never;
      }
      if (folderId === "sd-sub") {
        expect(driveId).toBe("sd-1");
        return Promise.resolve({ folders: [], video_files: [] }) as never;
      }
      return Promise.reject(new Error(`unexpected folder ${folderId}`)) as never;
    });
    mocked.driveScan.mockImplementation((ids: string[], _exc: string[] = [], driveId?: string) =>
      Promise.resolve({
        ok: true,
        videos: driveId === "sd-1" ? 3 : 5,
        stats: { files: ids.length, errors: 0 },
      }) as never
    );

    renderPage();
    await flush();
    await flush(); // let the roots query fire after the status resolves

    const myDriveButton = screen.getByText("My Drive");
    const sdRootButton = screen.getByText("Shared Drive One");
    act(() => { myDriveButton.click(); });
    act(() => { sdRootButton.click(); });
    await flush();

    // select both roots via their checkboxes
    const checkboxes = screen.getAllByRole("checkbox");
    // roots render first: [My Drive, Shared Drive One]
    act(() => {
      checkboxes[0].click();
      checkboxes[1].click();
    });
    await flush();

    act(() => {
      screen.getByRole("button", { name: /Scan 2 folder/ }).click();
    });
    await flush();

    // two scan calls: one unscoped (My Drive), one scoped to the Shared Drive
    expect(mocked.driveScan).toHaveBeenCalledTimes(2);
    expect(mocked.driveScan).toHaveBeenCalledWith(["root"], [], undefined);
    expect(mocked.driveScan).toHaveBeenCalledWith(["sd-1"], [], "sd-1");
    // aggregated result is displayed
    expect(screen.getByText(/8/)).toBeInTheDocument();
  });
});
