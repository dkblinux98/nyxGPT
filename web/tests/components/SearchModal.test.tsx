import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { SearchModal } from '../../src/components/SearchModal';

// Mock ToastContext
const mockToast = {
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
};

vi.mock('../../src/contexts/ToastContext', () => ({
  useToast: () => mockToast,
}));

describe('SearchModal', () => {
  const mockOnClose = vi.fn();
  const mockOnResultClick = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    global.fetch = vi.fn();
  });

  it('does not render when isOpen is false', () => {
    const { container } = render(
      <SearchModal
        isOpen={false}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    expect(container.firstChild).toBeNull();
  });

  it('renders modal when isOpen is true', () => {
    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    expect(screen.getByText('Search Messages')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('Search message content...')).toBeInTheDocument();
    expect(screen.getByLabelText('Case sensitive')).toBeInTheDocument();
  });

  it('auto-focuses search input when modal opens', () => {
    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...');
    expect(searchInput).toHaveFocus();
  });

  it('calls onClose when close button is clicked', async () => {
    const user = userEvent.setup();
    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const closeButton = screen.getByLabelText('Close search');
    await user.click(closeButton);

    expect(mockOnClose).toHaveBeenCalledTimes(1);
  });

  it('calls onClose when backdrop is clicked', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    // Find the backdrop (first div with position: fixed)
    const backdrop = container.querySelector('div[style*="position: fixed"]');
    expect(backdrop).toBeInTheDocument();

    await user.click(backdrop!);

    expect(mockOnClose).toHaveBeenCalledTimes(1);
  });

  it('calls onClose when Escape key is pressed', async () => {
    const user = userEvent.setup();
    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    await user.keyboard('{Escape}');

    expect(mockOnClose).toHaveBeenCalledTimes(1);
  });

  it('performs search with correct API parameters', async () => {
    const user = userEvent.setup({ delay: null });

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        query: 'test query',
        total_results: 0,
        results: [],
      }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'test query');

    // Wait for debounce (300ms)
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('/api/v1/sessions/search?query=test+query')
      );
    }, { timeout: 500 });
  });

  it('displays search results correctly', async () => {
    const user = userEvent.setup({ delay: null });

    const mockResults = [
      {
        session_name: 'test-session',
        session_title: 'Test Session',
        message_index: 0,
        role: 'user',
        content: 'Full message content here',
        content_preview: 'This is a preview with test query highlighted',
        timestamp: '2024-01-01T12:00:00Z',
        matches: 1,
      },
      {
        session_name: 'another-session',
        session_title: 'Another Session',
        message_index: 5,
        role: 'assistant',
        content: 'Another message',
        content_preview: 'Another preview with test query',
        timestamp: null,
        matches: 2,
      },
    ];

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        query: 'test query',
        total_results: 2,
        results: mockResults,
      }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'test query');

    await waitFor(() => {
      expect(screen.getByText('Test Session')).toBeInTheDocument();
      expect(screen.getByText('Another Session')).toBeInTheDocument();
      expect(screen.getByText('2 results')).toBeInTheDocument();
    });

    // Check for role icons
    expect(screen.getByText('👤')).toBeInTheDocument(); // user
    expect(screen.getByText('🤖')).toBeInTheDocument(); // assistant

    // Check for match badge
    expect(screen.getByText('2 matches')).toBeInTheDocument();
  });

  it('handles empty search results', async () => {
    const user = userEvent.setup({ delay: null });

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        query: 'nonexistent',
        total_results: 0,
        results: [],
      }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'nonexistent');

    await waitFor(() => {
      expect(screen.getByText(/No messages found matching/)).toBeInTheDocument();
      expect(screen.getByText('No results')).toBeInTheDocument();
    });
  });

  it('handles API errors gracefully', async () => {
    const user = userEvent.setup({ delay: null });

    (global.fetch as any).mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'Internal server error' }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'test');

    await waitFor(() => {
      expect(mockToast.error).toHaveBeenCalledWith(
        expect.stringContaining('Search failed')
      );
    });
  });

  it('calls onResultClick with correct parameters when result is clicked', async () => {
    const user = userEvent.setup({ delay: null });

    const mockResult = {
      session_name: 'test-session',
      session_title: 'Test Session',
      message_index: 5,
      role: 'user',
      content: 'Full message content',
      content_preview: 'Preview text',
      timestamp: null,
      matches: 1,
    };

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        query: 'test',
        total_results: 1,
        results: [mockResult],
      }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'test');

    await waitFor(() => {
      expect(screen.getByText('Test Session')).toBeInTheDocument();
    });

    const resultCard = screen.getByText('Test Session').closest('div[style*="cursor: pointer"]');
    expect(resultCard).toBeInTheDocument();

    await user.click(resultCard!);

    expect(mockOnResultClick).toHaveBeenCalledWith('test-session', 5);
    expect(mockOnClose).toHaveBeenCalledTimes(1);
  });

  it('applies case-sensitive filter correctly', async () => {
    const user = userEvent.setup({ delay: null });

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        query: 'Test',
        total_results: 0,
        results: [],
      }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const caseCheckbox = screen.getByLabelText('Case sensitive');
    await user.click(caseCheckbox);

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'Test');

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('case_sensitive=true')
      );
    });
  });

  it('applies role filter correctly', async () => {
    const user = userEvent.setup({ delay: null });

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        query: 'test',
        total_results: 0,
        results: [],
      }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const roleSelect = screen.getByDisplayValue('All');
    await user.selectOptions(roleSelect, 'assistant');

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'test');

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining('role_filter=assistant')
      );
    });
  });

  it('highlights query matches in preview text', async () => {
    const user = userEvent.setup({ delay: null });

    const mockResult = {
      session_name: 'test-session',
      session_title: 'Test Session',
      message_index: 0,
      role: 'user',
      content: 'This is a test message',
      content_preview: 'This is a test message',
      timestamp: null,
      matches: 1,
    };

    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        query: 'test',
        total_results: 1,
        results: [mockResult],
      }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'test');

    await waitFor(() => {
      expect(screen.getByText('Test Session')).toBeInTheDocument();
    });

    // Check that the preview contains a <mark> element for highlighting
    const marks = screen.getAllByText('test');
    const highlightedMark = marks.find(el => el.tagName === 'MARK');
    expect(highlightedMark).toBeInTheDocument();
  });

  it('shows loading state while searching', async () => {
    const user = userEvent.setup({ delay: null });

    // Create a promise that won't resolve immediately
    let resolveSearch: any;
    const searchPromise = new Promise((resolve) => {
      resolveSearch = resolve;
    });

    (global.fetch as any).mockReturnValueOnce(searchPromise);

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...');
    await user.type(searchInput, 'test');

    // Should show loading spinner
    await waitFor(() => {
      expect(screen.getByText('Searching...')).toBeInTheDocument();
    }, { timeout: 500 });

    // Resolve the search
    resolveSearch({
      ok: true,
      json: async () => ({
        query: 'test',
        total_results: 0,
        results: [],
      }),
    });

    // Loading should disappear
    await waitFor(() => {
      expect(screen.queryByText('Searching...')).not.toBeInTheDocument();
    });
  });

  it('displays initial empty state before search', () => {
    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    expect(screen.getByText('Enter a search term to find messages across all sessions')).toBeInTheDocument();
  });

  it('clears results when search query is cleared', async () => {
    const user = userEvent.setup({ delay: null });

    // First search with results
    (global.fetch as any).mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        query: 'test',
        total_results: 1,
        results: [{
          session_name: 'test-session',
          session_title: 'Test',
          message_index: 0,
          role: 'user',
          content: 'test',
          content_preview: 'test',
          timestamp: null,
          matches: 1,
        }],
      }),
    });

    render(
      <SearchModal
        isOpen={true}
        onClose={mockOnClose}
        onResultClick={mockOnResultClick}
      />
    );

    const searchInput = screen.getByPlaceholderText('Search message content...') as HTMLInputElement;
    await user.type(searchInput, 'test');

    await waitFor(() => {
      expect(screen.getByText('Test')).toBeInTheDocument();
    });

    // Clear the input
    await user.clear(searchInput);

    // Should show initial state again
    await waitFor(() => {
      expect(screen.getByText('Enter a search term to find messages across all sessions')).toBeInTheDocument();
    });
  });
});
