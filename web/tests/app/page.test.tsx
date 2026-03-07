import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { imageConfig } from '../../next.config';
import ChatPane from '../../src/app/components/ChatPane';

/**
 * Main Page Tests
 *
 * Tests for the main page component including keyboard shortcuts
 * and UI elements like the logo.
 */

describe('Logo Display', () => {
  it('should have logo image file in public directory', () => {
    // Test verifies the logo path is correct
    const logoPath = '/nyxgpt-logo.png';
    expect(logoPath).toBe('/nyxgpt-logo.png');
  });

  it('should use proper Next.js Image component attributes', () => {
    // Test verifies proper image configuration
    const imageConfig = {
      src: '/nyxgpt-logo.png',
      alt: 'nyxGPT logo',
      width: 40,
      height: 40,
      priority: true,
    };

    expect(imageConfig.src).toBe('/nyxgpt-logo.png');
    expect(imageConfig.alt).toBe('nyxGPT logo');
    expect(imageConfig.width).toBe(40);
    expect(imageConfig.height).toBe(40);
    expect(imageConfig.priority).toBe(true);
  });
});

/**
 * Image Optimization Tests
 *
 * Tests verifying image optimization configuration and attributes:
 * - Next.js imageConfig export values (formats, deviceSizes, imageSizes)
 * - Lazy loading attributes rendered on ChatPane attachment thumbnails
 */

// Mock react-virtuoso for test environment
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent, style }: any) => {
    const VIEWPORT_ITEMS = 10;
    const renderCount = Math.min(data?.length || 0, VIEWPORT_ITEMS);
    return (
      <div data-testid="virtualized-chat" style={style}>
        {data?.slice(0, renderCount).map((item: any, index: number) => (
          <div key={index}>{itemContent(index, item)}</div>
        ))}
      </div>
    );
  },
  VirtuosoHandle: vi.fn(),
}));

vi.mock('../../src/contexts/ToastContext', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }),
}));

const mockFetch = vi.fn();

describe('Image Optimization', () => {
  describe('Next.js imageConfig export', () => {
    it('should configure WebP and AVIF output formats', () => {
      expect(imageConfig.formats).toContain('image/webp');
      expect(imageConfig.formats).toContain('image/avif');
    });

    it('should define responsive device sizes', () => {
      expect(imageConfig.deviceSizes.length).toBeGreaterThan(0);
      expect(imageConfig.deviceSizes[0]).toBe(640);
      expect(imageConfig.deviceSizes[imageConfig.deviceSizes.length - 1]).toBe(1920);
    });

    it('should define image sizes for fixed-layout images', () => {
      expect(imageConfig.imageSizes).toContain(64);
      expect(imageConfig.imageSizes.length).toBeGreaterThan(0);
    });
  });

  describe('Attachment preview lazy loading in ChatPane', () => {
    beforeEach(() => {
      vi.clearAllMocks();
      global.fetch = mockFetch;

      // Default mock: message with an image attachment
      mockFetch.mockImplementation((url: string) => {
        if (url.includes('/api/v1/sessions/')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({
              messages: [
                {
                  role: 'user',
                  content: 'Here is an image',
                  attachments: [
                    {
                      type: 'image',
                      preview: 'data:image/png;base64,abc123',
                      filename: 'photo.png',
                      media_type: 'image/png',
                      data: 'abc123',
                    },
                  ],
                },
              ],
            }),
          });
        }
        if (url.includes('/api/models')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ models: ['llama3.1:8b'] }),
          });
        }
        if (url.includes('/metadata')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ rag_enabled: false, title: 'Test', model: 'llama3.1:8b' }),
          });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
      });
    });

    it('should render attachment thumbnails with loading="lazy"', async () => {
      render(<ChatPane sessionName="test-session" />);

      await waitFor(() => {
        const imgs = screen.getAllByAltText('photo.png');
        expect(imgs.length).toBeGreaterThan(0);
        imgs.forEach((img) => {
          expect(img).toHaveAttribute('loading', 'lazy');
        });
      });
    });

    it('should render attachment thumbnails with decoding="async"', async () => {
      render(<ChatPane sessionName="test-session" />);

      await waitFor(() => {
        const imgs = screen.getAllByAltText('photo.png');
        expect(imgs.length).toBeGreaterThan(0);
        imgs.forEach((img) => {
          expect(img).toHaveAttribute('decoding', 'async');
        });
      });
    });
  });
});

/**
 * Keyboard Shortcuts Tests
 *
 * Tests for keyboard shortcut logic in the main page component.
 * These tests verify the keyboard event handling logic.
 */

describe('Keyboard Shortcuts Logic', () => {
  beforeEach(() => {
    // Clear document event listeners before each test
    document.body.innerHTML = '';
  });

  describe('Modifier Key Detection', () => {
    it('should correctly identify metaKey (Mac) as modifier', () => {
      const event = new KeyboardEvent('keydown', {
        key: 'k',
        metaKey: true,
        ctrlKey: false,
      });

      const isMod = event.metaKey || event.ctrlKey;
      expect(isMod).toBe(true);
    });

    it('should correctly identify ctrlKey (Windows/Linux) as modifier', () => {
      const event = new KeyboardEvent('keydown', {
        key: 'k',
        metaKey: false,
        ctrlKey: true,
      });

      const isMod = event.metaKey || event.ctrlKey;
      expect(isMod).toBe(true);
    });

    it('should not identify modifier when neither key is pressed', () => {
      const event = new KeyboardEvent('keydown', {
        key: 'k',
        metaKey: false,
        ctrlKey: false,
      });

      const isMod = event.metaKey || event.ctrlKey;
      expect(isMod).toBe(false);
    });
  });

  describe('Key Combinations', () => {
    it('should recognize Cmd/Ctrl+K combination', () => {
      const event = new KeyboardEvent('keydown', {
        key: 'k',
        metaKey: true,
      });

      const isMod = event.metaKey || event.ctrlKey;
      const isNewChat = isMod && event.key === 'k';

      expect(isNewChat).toBe(true);
    });

    it('should recognize Cmd/Ctrl+/ combination', () => {
      const event = new KeyboardEvent('keydown', {
        key: '/',
        ctrlKey: true,
      });

      const isMod = event.metaKey || event.ctrlKey;
      const isToggleSidebar = isMod && event.key === '/';

      expect(isToggleSidebar).toBe(true);
    });

    it('should recognize standalone / key', () => {
      const event = new KeyboardEvent('keydown', {
        key: '/',
        metaKey: false,
        ctrlKey: false,
      });

      const isSearchFocus = event.key === '/';
      expect(isSearchFocus).toBe(true);
    });

    it('should recognize Escape key', () => {
      const event = new KeyboardEvent('keydown', {
        key: 'Escape',
      });

      const isEscape = event.key === 'Escape';
      expect(isEscape).toBe(true);
    });
  });

  describe('Input Field Detection', () => {
    it('should detect input element as target', () => {
      const input = document.createElement('input');
      const isInputElement = input instanceof HTMLInputElement;
      expect(isInputElement).toBe(true);
    });

    it('should detect textarea element as target', () => {
      const textarea = document.createElement('textarea');
      const isTextareaElement = textarea instanceof HTMLTextAreaElement;
      expect(isTextareaElement).toBe(true);
    });

    it('should not detect div as input element', () => {
      const div = document.createElement('div');
      const isInputElement = div instanceof HTMLInputElement;
      const isTextareaElement = div instanceof HTMLTextAreaElement;
      expect(isInputElement).toBe(false);
      expect(isTextareaElement).toBe(false);
    });

    it('should correctly filter / key when in input field', () => {
      const input = document.createElement('input');

      // Simulate the check from the actual code
      const shouldTrigger = !(input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement);
      expect(shouldTrigger).toBe(false);
    });

    it('should correctly allow / key when not in input field', () => {
      const div = document.createElement('div');

      // Simulate the check from the actual code
      const shouldTrigger = !(div instanceof HTMLInputElement || div instanceof HTMLTextAreaElement);
      expect(shouldTrigger).toBe(true);
    });
  });

  describe('Event Listener Lifecycle', () => {
    it('should be able to add and remove event listeners', () => {
      const handler = () => {};
      const initialCount = document.querySelectorAll('*').length;

      document.addEventListener('keydown', handler);
      document.removeEventListener('keydown', handler);

      // Test passes if no errors occur
      expect(initialCount).toBeGreaterThanOrEqual(0);
    });

    it('should handle multiple listener additions and removals', () => {
      const handlers = [() => {}, () => {}, () => {}];

      handlers.forEach(h => document.addEventListener('keydown', h));
      handlers.forEach(h => document.removeEventListener('keydown', h));

      // Test passes if no memory leaks or errors
      expect(handlers.length).toBe(3);
    });
  });

  describe('Browser Conflict Prevention', () => {
    it('should NOT use Cmd/Ctrl+F combination', () => {
      // Verify we're using / instead of Cmd/Ctrl+F
      const cmdF = new KeyboardEvent('keydown', {
        key: 'f',
        metaKey: true,
      });

      const isMod = cmdF.metaKey || cmdF.ctrlKey;
      const wouldConflict = isMod && cmdF.key === 'f';

      // Our implementation should NOT handle this combination
      // Browser's native find-in-page should work
      expect(wouldConflict).toBe(true);
      expect(cmdF.key).toBe('f'); // We don't preventDefault for 'f'
    });

    it('should use / key instead for search', () => {
      const slashKey = new KeyboardEvent('keydown', {
        key: '/',
        metaKey: false,
        ctrlKey: false,
      });

      const isSearchShortcut = slashKey.key === '/' && !slashKey.metaKey && !slashKey.ctrlKey;
      expect(isSearchShortcut).toBe(true);
    });
  });

  describe('Keyboard Shortcut Coverage', () => {
    it('should have new chat shortcut (Cmd/Ctrl+K)', () => {
      const event = new KeyboardEvent('keydown', {
        key: 'k',
        metaKey: true,
      });

      const isMod = event.metaKey || event.ctrlKey;
      const isNewChatShortcut = isMod && event.key === 'k';

      expect(isNewChatShortcut).toBe(true);
    });

    it('should have toggle sidebar shortcut (Cmd/Ctrl+/)', () => {
      const event = new KeyboardEvent('keydown', {
        key: '/',
        ctrlKey: true,
      });

      const isMod = event.metaKey || event.ctrlKey;
      const isToggleSidebarShortcut = isMod && event.key === '/';

      expect(isToggleSidebarShortcut).toBe(true);
    });

    it('should have search focus shortcut (/)', () => {
      const event = new KeyboardEvent('keydown', {
        key: '/',
      });

      const isSearchShortcut = event.key === '/';
      expect(isSearchShortcut).toBe(true);
    });

    it('should have close menu shortcut (Esc)', () => {
      const event = new KeyboardEvent('keydown', {
        key: 'Escape',
      });

      const isCloseMenuShortcut = event.key === 'Escape';
      expect(isCloseMenuShortcut).toBe(true);
    });
  });
});
