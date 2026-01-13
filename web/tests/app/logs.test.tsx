import { describe, it, expect } from 'vitest';

/**
 * Logs API Routes Tests
 *
 * Tests for the log viewer API proxy routes that forward requests
 * from the Next.js frontend to the FastAPI backend.
 */

describe('Logs API Routes', () => {
  describe('GET /api/v1/logs/files', () => {
    it('should return list of log files', async () => {
      const response = await fetch('/api/v1/logs/files');
      expect(response.status).toBe(200);

      const data = await response.json();
      expect(data).toHaveProperty('files');
      expect(Array.isArray(data.files)).toBe(true);
      expect(data.files.length).toBeGreaterThan(0);
    });

    it('should include file metadata', async () => {
      const response = await fetch('/api/v1/logs/files');
      const data = await response.json();

      const firstFile = data.files[0];
      expect(firstFile).toHaveProperty('name');
      expect(firstFile).toHaveProperty('path');
      expect(firstFile).toHaveProperty('size');
      expect(firstFile).toHaveProperty('modified');
    });

    it('should include log directory path', async () => {
      const response = await fetch('/api/v1/logs/files');
      const data = await response.json();

      expect(data).toHaveProperty('log_dir');
      expect(typeof data.log_dir).toBe('string');
    });
  });

  describe('GET /api/v1/logs/view/:filename', () => {
    it('should return log file contents', async () => {
      const response = await fetch('/api/v1/logs/view/mygpt.log');
      expect(response.status).toBe(200);

      const data = await response.json();
      expect(data).toHaveProperty('filename');
      expect(data).toHaveProperty('lines');
      expect(Array.isArray(data.lines)).toBe(true);
    });

    it('should include line count metadata', async () => {
      const response = await fetch('/api/v1/logs/view/mygpt.log');
      const data = await response.json();

      expect(data).toHaveProperty('total_lines');
      expect(data).toHaveProperty('filtered_lines');
      expect(typeof data.total_lines).toBe('number');
      expect(typeof data.filtered_lines).toBe('number');
    });

    it('should support tail parameter', async () => {
      const response = await fetch('/api/v1/logs/view/mygpt.log?tail=2');
      const data = await response.json();

      expect(data.lines.length).toBe(2);
    });

    it('should support level filter parameter', async () => {
      const response = await fetch('/api/v1/logs/view/mygpt.log?level=ERROR');
      const data = await response.json();

      // All returned lines should contain 'ERROR'
      data.lines.forEach((line: string) => {
        expect(line).toContain('ERROR');
      });
    });

    it('should support search parameter', async () => {
      const response = await fetch('/api/v1/logs/view/mygpt.log?search=Server');
      const data = await response.json();

      // All returned lines should contain the search term
      data.lines.forEach((line: string) => {
        expect(line.toLowerCase()).toContain('server');
      });
    });

    it('should support combined filters', async () => {
      const response = await fetch('/api/v1/logs/view/mygpt.log?level=INFO&tail=1');
      const data = await response.json();

      expect(data.lines.length).toBeLessThanOrEqual(1);
      if (data.lines.length > 0) {
        expect(data.lines[0]).toContain('INFO');
      }
    });
  });

  describe('GET /api/v1/logs/stream/:filename', () => {
    it('should return log file as plain text', async () => {
      const response = await fetch('/api/v1/logs/stream/mygpt.log');
      expect(response.status).toBe(200);

      const contentType = response.headers.get('Content-Type');
      expect(contentType).toBe('text/plain');
    });

    it('should include Content-Disposition header', async () => {
      const response = await fetch('/api/v1/logs/stream/mygpt.log');

      const disposition = response.headers.get('Content-Disposition');
      expect(disposition).toBeTruthy();
      expect(disposition).toContain('mygpt.log');
    });

    it('should include security header', async () => {
      const response = await fetch('/api/v1/logs/stream/mygpt.log');

      const nosniff = response.headers.get('X-Content-Type-Options');
      expect(nosniff).toBe('nosniff');
    });

    it('should return log content as text', async () => {
      const response = await fetch('/api/v1/logs/stream/mygpt.log');
      const text = await response.text();

      expect(typeof text).toBe('string');
      expect(text.length).toBeGreaterThan(0);
    });
  });

  describe('Query Parameter Encoding', () => {
    it('should properly encode filename with special characters', async () => {
      const filename = 'test log.txt';
      const response = await fetch(`/api/v1/logs/view/${encodeURIComponent(filename)}`);

      // Should not throw an error
      expect(response.status).toBeGreaterThanOrEqual(200);
    });

    it('should properly encode search parameter with special characters', async () => {
      const search = 'test & query';
      const response = await fetch(
        `/api/v1/logs/view/mygpt.log?search=${encodeURIComponent(search)}`
      );

      expect(response.status).toBe(200);
    });
  });

  describe('Error Handling', () => {
    it('should handle response from backend gracefully', async () => {
      // Test that we properly forward responses without modifying them
      const response = await fetch('/api/v1/logs/files');

      // Should have proper headers
      expect(response.headers.get('Content-Type')).toBeTruthy();

      // Should be able to parse response
      const data = await response.json();
      expect(data).toBeDefined();
    });
  });
});
