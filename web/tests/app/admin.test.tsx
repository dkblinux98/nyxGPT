import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import AdminPage from '../../src/app/admin/page';

/**
 * Configuration Wizard Tests
 *
 * Comprehensive tests for the admin configuration wizard component.
 * Tests cover component rendering, state management, API interactions,
 * form validation, and complete user workflows.
 */

describe('AdminPage Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('Component Rendering and Initial State', () => {
    it('should render loading spinner on initial load', () => {
      render(<AdminPage />);
      expect(screen.getByRole('status')).toBeInTheDocument();
      expect(screen.getByText('Loading configuration...')).toBeInTheDocument();
    });

    it('should load and display configuration data', async () => {
      server.use(
        http.get('/api/config', () => {
          return HttpResponse.json({
            ollama_base_url: 'http://127.0.0.1:11434',
            default_model: 'llama3.1:8b',
            rag_enabled: false,
            log_level: 'INFO',
          });
        })
      );

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      expect(screen.getAllByText('Model Selection')[0]).toBeInTheDocument();
    });

    it('should display error message when config loading fails', async () => {
      server.use(
        http.get('/api/config', () => {
          return HttpResponse.json(
            { detail: 'Internal server error' },
            { status: 500 }
          );
        })
      );

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Failed to load configuration')).toBeInTheDocument();
      });
    });
  });

  describe('Step Navigation', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/config', () => {
          return HttpResponse.json({
            ollama_base_url: 'http://127.0.0.1:11434',
            default_model: 'llama3.1:8b',
            rag_enabled: false,
            log_level: 'INFO',
          });
        })
      );
    });

    it('should start at model selection step', async () => {
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Model Selection')[0]).toBeInTheDocument();
      });

      expect(screen.getByText('Select Default Model')).toBeInTheDocument();
    });

    it('should navigate to next step when Next button clicked', async () => {
      const user = userEvent.setup();
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      const nextButton = screen.getByRole('button', { name: /next/i });
      await user.click(nextButton);

      expect(screen.getAllByText('RAG Configuration').length).toBeGreaterThan(0);
      expect(screen.getByText('Configure retrieval-augmented generation')).toBeInTheDocument();
    });

    it('should disable Previous button on first step', async () => {
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      const prevButton = screen.getByRole('button', { name: /previous/i });
      expect(prevButton).toBeDisabled();
    });

    it('should disable Next button on last step', async () => {
      const user = userEvent.setup();
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      // Navigate to last step (summary)
      const nextButton = screen.getByRole('button', { name: /next/i });
      await user.click(nextButton); // RAG
      await user.click(nextButton); // API
      await user.click(nextButton); // Summary

      expect(screen.getAllByText('Summary')[0]).toBeInTheDocument();
      expect(nextButton).toBeDisabled();
    });
  });

  describe('API Interactions', () => {
    it('should handle connection test successfully', async () => {
      server.use(
        http.get('/api/config', () => {
          return HttpResponse.json({
            ollama_base_url: 'http://127.0.0.1:11434',
            default_model: 'llama3.1:8b',
            rag_enabled: false,
            log_level: 'INFO',
          });
        }),
        http.get('/api/info', () => {
          return HttpResponse.json({ status: 'ok' });
        })
      );

      const user = userEvent.setup();
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      // Navigate to API settings step
      const nextButton = screen.getByRole('button', { name: /next/i });
      await user.click(nextButton); // RAG
      await user.click(nextButton); // API

      const testButton = screen.getByRole('button', { name: /test connection/i });
      await user.click(testButton);

      await waitFor(() => {
        expect(screen.getByText(/connection successful/i)).toBeInTheDocument();
      });
    });

    it('should handle connection test failure', async () => {
      server.use(
        http.get('/api/config', () => {
          return HttpResponse.json({
            ollama_base_url: 'http://127.0.0.1:11434',
            default_model: 'llama3.1:8b',
            rag_enabled: false,
            log_level: 'INFO',
          });
        }),
        http.get('/api/info', () => {
          return HttpResponse.json(
            { detail: 'Service unavailable' },
            { status: 503 }
          );
        })
      );

      const user = userEvent.setup();
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      // Navigate to API settings step
      const nextButton = screen.getByRole('button', { name: /next/i });
      await user.click(nextButton); // RAG
      await user.click(nextButton); // API

      const testButton = screen.getByRole('button', { name: /test connection/i });
      await user.click(testButton);

      await waitFor(() => {
        expect(screen.getByText(/connection failed/i)).toBeInTheDocument();
      });
    });
  });

  describe('Configuration Save', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/config', () => {
          return HttpResponse.json({
            ollama_base_url: 'http://127.0.0.1:11434',
            default_model: 'llama3.1:8b',
            rag_enabled: false,
            log_level: 'INFO',
          });
        })
      );
    });

    it('should save configuration successfully', async () => {
      server.use(
        http.post('/api/config', async ({ request }) => {
          const body = await request.json();
          expect(body).toHaveProperty('default_model');
          expect(body).toHaveProperty('rag_enabled');
          expect(body).toHaveProperty('log_level');
          expect(body).not.toHaveProperty('ollama_base_url'); // Read-only field
          return HttpResponse.json({ success: true });
        })
      );

      const user = userEvent.setup();
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      // Navigate to summary step
      const nextButton = screen.getByRole('button', { name: /next/i });
      await user.click(nextButton); // RAG
      await user.click(nextButton); // API
      await user.click(nextButton); // Summary

      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      await user.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText(/configuration saved successfully/i)).toBeInTheDocument();
      });
    });

    it('should handle save failure', async () => {
      server.use(
        http.post('/api/config', () => {
          return HttpResponse.json(
            { detail: 'Validation error' },
            { status: 400 }
          );
        })
      );

      const user = userEvent.setup();
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      // Navigate to summary step
      const nextButton = screen.getByRole('button', { name: /next/i });
      await user.click(nextButton); // RAG
      await user.click(nextButton); // API
      await user.click(nextButton); // Summary

      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      await user.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText('Failed to save configuration')).toBeInTheDocument();
      });
    });
  });

  describe('Form State Management', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/config', () => {
          return HttpResponse.json({
            ollama_base_url: 'http://127.0.0.1:11434',
            default_model: 'llama3.1:8b',
            rag_enabled: false,
            log_level: 'INFO',
          });
        })
      );
    });

    it('should toggle RAG enabled state', async () => {
      const user = userEvent.setup();
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });

      // Navigate to RAG step
      const nextButton = screen.getByRole('button', { name: /next/i });
      await user.click(nextButton);

      const ragCheckbox = screen.getByRole('checkbox', { name: /enable rag/i });
      expect(ragCheckbox).not.toBeChecked();

      await user.click(ragCheckbox);
      expect(ragCheckbox).toBeChecked();
    });
  });

  describe('Error Recovery', () => {
    it('should allow retry when initial load fails', async () => {
      let requestCount = 0;

      server.use(
        http.get('/api/config', () => {
          requestCount++;
          if (requestCount === 1) {
            return HttpResponse.json(
              { detail: 'Service unavailable' },
              { status: 503 }
            );
          }
          return HttpResponse.json({
            ollama_base_url: 'http://127.0.0.1:11434',
            default_model: 'llama3.1:8b',
            rag_enabled: false,
            log_level: 'INFO',
          });
        })
      );

      const user = userEvent.setup();
      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Failed to load configuration')).toBeInTheDocument();
      });

      const retryButton = screen.getByRole('button', { name: /retry/i });
      await user.click(retryButton);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
        expect(screen.getAllByText('Model Selection')[0]).toBeInTheDocument();
      });
    });
  });
});
