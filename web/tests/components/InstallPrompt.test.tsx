import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import InstallPrompt from "../../src/components/InstallPrompt";

// Mock the useInstallPrompt hook
vi.mock("../../src/hooks/useInstallPrompt", () => ({
  useInstallPrompt: vi.fn(() => ({
    isInstallable: true,
    isInstalled: false,
    promptInstall: vi.fn().mockResolvedValue(true),
  })),
}));

describe("InstallPrompt", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("should render install prompt when installable", () => {
    render(<InstallPrompt />);

    expect(screen.getByText("Install nyxGPT")).toBeInTheDocument();
    expect(
      screen.getByText("Install the app for a better experience")
    ).toBeInTheDocument();
    expect(screen.getByText("Not now")).toBeInTheDocument();
    expect(screen.getByText("Install")).toBeInTheDocument();
  });

  it("should not render when not installable", async () => {
    const { useInstallPrompt } = await import(
      "../../src/hooks/useInstallPrompt"
    );
    vi.mocked(useInstallPrompt).mockReturnValue({
      isInstallable: false,
      isInstalled: false,
      promptInstall: vi.fn().mockResolvedValue(false),
    });

    const { container } = render(<InstallPrompt />);

    expect(container.firstChild).toBeNull();
  });

  it("should not render when already installed", async () => {
    const { useInstallPrompt } = await import(
      "../../src/hooks/useInstallPrompt"
    );
    vi.mocked(useInstallPrompt).mockReturnValue({
      isInstallable: false,
      isInstalled: true,
      promptInstall: vi.fn().mockResolvedValue(false),
    });

    const { container } = render(<InstallPrompt />);

    expect(container.firstChild).toBeNull();
  });

  it("should dismiss prompt when clicking 'Not now'", async () => {
    const user = userEvent.setup();
    const { container } = render(<InstallPrompt />);

    const dismissButton = screen.getByText("Not now");
    await user.click(dismissButton);

    expect(container.firstChild).toBeNull();
  });

  it("should call promptInstall when clicking 'Install'", async () => {
    const user = userEvent.setup();
    const mockPromptInstall = vi.fn().mockResolvedValue(true);

    const { useInstallPrompt } = await import(
      "../../src/hooks/useInstallPrompt"
    );
    vi.mocked(useInstallPrompt).mockReturnValue({
      isInstallable: true,
      isInstalled: false,
      promptInstall: mockPromptInstall,
    });

    render(<InstallPrompt />);

    const installButton = screen.getByText("Install");
    await user.click(installButton);

    expect(mockPromptInstall).toHaveBeenCalled();
  });
});
