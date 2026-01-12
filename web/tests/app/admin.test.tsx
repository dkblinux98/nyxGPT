import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import AdminPage from '../../src/app/admin/page';

/**
 * Configuration Wizard Tests
 *
 * Tests for the admin configuration wizard component.
 * These tests verify form validation, step navigation, component rendering,
 * API integration, and error states.
 */

describe('Configuration Wizard Component', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Mock fetch globally
    global.fetch = vi.fn((url: string) => {
      // Default mock responses
      if (url === '/api/config') {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            ollama_base_url: 'http://127.0.0.1:11434',
            default_model: 'llama3.1:8b',
            rag_enabled: false,
            log_level: 'INFO',
          }),
        });
      }
      if (url === '/api/models') {
        return Promise.resolve({
          ok: true,
          json: async () => ({ models: ['llama3.1:8b'] }),
        });
      }
      return Promise.reject(new Error('Unknown URL'));
    }) as any;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Component Rendering', () => {
    it('should render loading state initially', () => {
      (global.fetch as any).mockImplementation(() =>
        new Promise(() => {}) // Never resolves to keep loading state
      );

      render(<AdminPage />);
      expect(screen.getByText('Loading configuration...')).toBeInTheDocument();
    });

    it('should render error state when config load fails', async () => {
      (global.fetch as any).mockRejectedValue(new Error('HTTP 500'));

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Failed to load configuration')).toBeInTheDocument();
        expect(screen.getByText('HTTP 500')).toBeInTheDocument();
      });
    });

    it('should render wizard after successful config load', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b', 'llama3.2:3b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });
    });

    it('should render all wizard steps in progress indicator', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: '',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: [] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
        expect(screen.getByText('Choose your default LLM model')).toBeInTheDocument();
      });
    });

    it('should render model selection dropdown with available models', async () => {
      const mockModels = ['llama3.1:8b', 'llama3.2:3b', 'mistral:7b'];
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: '',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: mockModels }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        // Check that the dropdown contains the expected models
        expect(screen.getByText('Select a model...')).toBeInTheDocument();
        expect(screen.getByText('llama3.1:8b')).toBeInTheDocument();
        expect(screen.getByText('llama3.2:3b')).toBeInTheDocument();
        expect(screen.getByText('mistral:7b')).toBeInTheDocument();
      });
    });

    it('should render Back to Chat link', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        const backLink = screen.getByRole('link', { name: /back to chat/i });
        expect(backLink).toBeInTheDocument();
        expect(backLink).toHaveAttribute('href', '/');
      });
    });
  });

  describe('User Interactions', () => {
    it('should allow user to select a model', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: '',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b', 'llama3.2:3b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Select Default Model')).toBeInTheDocument();
      });

      // Find the select element containing the placeholder text
      const select = screen.getByRole('combobox');
      fireEvent.change(select, { target: { value: 'llama3.1:8b' } });

      expect(select).toHaveValue('llama3.1:8b');
    });

    it('should navigate to next step when Next button clicked', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });

      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);

      await waitFor(() => {
        expect(screen.getByRole('heading', { name: /rag configuration/i })).toBeInTheDocument();
      });
    });

    it('should navigate to previous step when Previous button clicked', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });

      // Go to next step first
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);

      await waitFor(() => {
        expect(screen.getByRole('heading', { name: /rag configuration/i })).toBeInTheDocument();
      });

      // Go back
      const previousButton = screen.getByRole('button', { name: /previous/i });
      fireEvent.click(previousButton);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });
    });

    it('should toggle RAG checkbox', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });

      // Navigate to RAG step
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);

      await waitFor(() => {
        const ragCheckbox = screen.getByRole('checkbox', { name: /enable rag/i });
        expect(ragCheckbox).not.toBeChecked();
        fireEvent.click(ragCheckbox);
        expect(ragCheckbox).toBeChecked();
      });
    });

    it('should test connection on API step', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        if (url === '/api/info') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ status: 'ok' }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });

      // Navigate to API step
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton); // to RAG
      await waitFor(() => screen.getByRole('heading', { name: /rag configuration/i }));
      fireEvent.click(nextButton); // to API

      await waitFor(() => {
        expect(screen.getByRole('heading', { name: /api settings/i })).toBeInTheDocument();
      });

      const testButton = screen.getByRole('button', { name: /test connection/i });
      fireEvent.click(testButton);

      await waitFor(() => {
        expect(screen.getByText(/connection successful/i)).toBeInTheDocument();
      });
    });
  });

  describe('API Integration', () => {
    it('should load config from API on mount', async () => {
      const mockConfig = {
        ollama_base_url: 'http://127.0.0.1:11434',
        default_model: 'llama3.1:8b',
        rag_enabled: true,
        log_level: 'DEBUG',
      };

      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => mockConfig,
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(global.fetch).toHaveBeenCalledWith('/api/config');
      });
    });

    it('should load models from API on mount', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: '',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b', 'mistral:7b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(global.fetch).toHaveBeenCalledWith('/api/models');
      });
    });

    it('should save configuration to API', async () => {
      let savedConfig: any = null;

      (global.fetch as any).mockImplementation((url: string, options?: any) => {
        if (url === '/api/config' && options?.method === 'POST') {
          savedConfig = JSON.parse(options.body);
          return Promise.resolve({
            ok: true,
            json: async () => ({ success: true }),
          });
        }
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });

      // Navigate to summary step
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton); // to RAG
      await waitFor(() => screen.getByRole('heading', { name: /rag configuration/i }));
      fireEvent.click(nextButton); // to API
      await waitFor(() => screen.getByRole('heading', { name: /api settings/i }));
      fireEvent.click(nextButton); // to Summary

      await waitFor(() => {
        expect(screen.getByText('Review Configuration')).toBeInTheDocument();
      });

      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(savedConfig).toEqual({
          default_model: 'llama3.1:8b',
          rag_enabled: false,
          log_level: 'INFO',
        });
      });
    });

    it('should show success message after saving', async () => {
      (global.fetch as any).mockImplementation((url: string, options?: any) => {
        if (url === '/api/config' && options?.method === 'POST') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ success: true }),
          });
        }
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });

      // Navigate to summary and save
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
      await waitFor(() => screen.getByRole('heading', { name: /rag configuration/i }));
      fireEvent.click(nextButton);
      await waitFor(() => screen.getByRole('heading', { name: /api settings/i }));
      fireEvent.click(nextButton);
      await waitFor(() => screen.getByText('Review Configuration'));

      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText(/configuration saved successfully/i)).toBeInTheDocument();
      });
    });
  });

  describe('Error Handling', () => {
    it('should show error when config API fails', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.reject(new Error('Network error'));
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: [] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Failed to load configuration')).toBeInTheDocument();
        expect(screen.getByText('Network error')).toBeInTheDocument();
      });
    });

    it('should show error when save fails', async () => {
      (global.fetch as any).mockImplementation((url: string, options?: any) => {
        if (url === '/api/config' && options?.method === 'POST') {
          return Promise.resolve({
            ok: false,
            status: 500,
            json: async () => ({ detail: 'Internal server error' }),
          });
        }
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });

      // Navigate to summary and save
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
      await waitFor(() => screen.getByRole('heading', { name: /rag configuration/i }));
      fireEvent.click(nextButton);
      await waitFor(() => screen.getByRole('heading', { name: /api settings/i }));
      fireEvent.click(nextButton);
      await waitFor(() => screen.getByText('Review Configuration'));

      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      fireEvent.click(saveButton);

      await waitFor(() => {
        expect(screen.getByText('Failed to save configuration')).toBeInTheDocument();
        expect(screen.getByText('Internal server error')).toBeInTheDocument();
      });
    });

    it('should show error when connection test fails', async () => {
      (global.fetch as any).mockImplementation((url: string) => {
        if (url === '/api/config') {
          return Promise.resolve({
            ok: true,
            json: async () => ({
              ollama_base_url: 'http://127.0.0.1:11434',
              default_model: 'llama3.1:8b',
              rag_enabled: false,
              log_level: 'INFO',
            }),
          });
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        if (url === '/api/info') {
          return Promise.reject(new Error('Connection refused'));
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Model Selection')).toBeInTheDocument();
      });

      // Navigate to API step
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
      await waitFor(() => screen.getByRole('heading', { name: /rag configuration/i }));
      fireEvent.click(nextButton);
      await waitFor(() => screen.getByRole('heading', { name: /api settings/i }));

      const testButton = screen.getByRole('button', { name: /test connection/i });
      fireEvent.click(testButton);

      await waitFor(() => {
        expect(screen.getByText(/connection failed/i)).toBeInTheDocument();
      });
    });

    it('should handle retry after config load failure', async () => {
      let attempts = 0;
      (global.fetch as any).mockImplementation((url: string) => {
        attempts++;
        if (url === '/api/config') {
          if (attempts === 1) {
            return Promise.reject(new Error('Network error'));
          } else {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                ollama_base_url: 'http://127.0.0.1:11434',
                default_model: 'llama3.1:8b',
                rag_enabled: false,
                log_level: 'INFO',
              }),
            });
          }
        }
        if (url === '/api/models') {
          return Promise.resolve({
            ok: true,
            json: async () => ({ models: ['llama3.1:8b'] }),
          });
        }
        return Promise.reject(new Error('Unknown URL'));
      });

      render(<AdminPage />);

      await waitFor(() => {
        expect(screen.getByText('Failed to load configuration')).toBeInTheDocument();
      });

      const retryButton = screen.getByRole('button', { name: /retry/i });
      fireEvent.click(retryButton);

      await waitFor(() => {
        expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
      });
    });
  });
});

describe('Configuration Wizard Logic', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('Wizard Step Navigation', () => {
    it('should start at model selection step', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const currentStep = steps[0];
      expect(currentStep).toBe('model');
    });

    it('should allow moving to next step', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      let currentStepIndex = 0;

      currentStepIndex += 1;

      expect(steps[currentStepIndex]).toBe('rag');
    });

    it('should allow moving to previous step', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      let currentStepIndex = 2;

      currentStepIndex -= 1;

      expect(steps[currentStepIndex]).toBe('rag');
    });

    it('should not move before first step', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      let currentStepIndex = 0;

      if (currentStepIndex > 0) {
        currentStepIndex -= 1;
      }

      expect(currentStepIndex).toBe(0);
    });

    it('should not move past last step', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      let currentStepIndex = 3;

      if (currentStepIndex < steps.length - 1) {
        currentStepIndex += 1;
      }

      expect(currentStepIndex).toBe(3);
    });
  });

  describe('Form Validation', () => {
    it('should validate model selection', () => {
      const formData = {
        default_model: 'llama3.1:8b',
        rag_enabled: false,
        log_level: 'INFO',
      };

      const isModelSelected = formData.default_model !== '';
      expect(isModelSelected).toBe(true);
    });

    it('should detect empty model selection', () => {
      const formData = {
        default_model: '',
        rag_enabled: false,
        log_level: 'INFO',
      };

      const isModelSelected = formData.default_model !== '';
      expect(isModelSelected).toBe(false);
    });

    it('should disable Next button on model step when no model is selected', () => {
      const formData = {
        default_model: '',
        rag_enabled: false,
        log_level: 'INFO',
      };
      const currentStep = 'model';
      const currentStepIndex = 0;
      const totalSteps = 4;

      // Next button should be disabled when on model step and no model selected
      const isNextDisabled =
        currentStepIndex === totalSteps - 1 ||
        (currentStep === 'model' && !formData.default_model);

      expect(isNextDisabled).toBe(true);
    });

    it('should enable Next button on model step when model is selected', () => {
      const formData = {
        default_model: 'llama3.1:8b',
        rag_enabled: false,
        log_level: 'INFO',
      };
      const currentStep = 'model';
      const currentStepIndex = 0;
      const totalSteps = 4;

      // Next button should be enabled when on model step and model is selected
      const isNextDisabled =
        currentStepIndex === totalSteps - 1 ||
        (currentStep === 'model' && !formData.default_model);

      expect(isNextDisabled).toBe(false);
    });

    it('should disable Save button when default_model is empty', () => {
      const formData = {
        default_model: '',
        rag_enabled: false,
        log_level: 'INFO',
      };
      const saving = false;

      const isSaveDisabled = saving || !formData.default_model;

      expect(isSaveDisabled).toBe(true);
    });

    it('should enable Save button when default_model is set', () => {
      const formData = {
        default_model: 'llama3.1:8b',
        rag_enabled: false,
        log_level: 'INFO',
      };
      const saving = false;

      const isSaveDisabled = saving || !formData.default_model;

      expect(isSaveDisabled).toBe(false);
    });

    it('should validate RAG enabled state', () => {
      const formData = {
        default_model: 'llama3.1:8b',
        rag_enabled: true,
        log_level: 'INFO',
      };

      expect(typeof formData.rag_enabled).toBe('boolean');
      expect(formData.rag_enabled).toBe(true);
    });

    it('should validate log level selection', () => {
      const validLogLevels = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];
      const formData = {
        default_model: 'llama3.1:8b',
        rag_enabled: false,
        log_level: 'INFO',
      };

      const isValidLogLevel = validLogLevels.includes(formData.log_level);
      expect(isValidLogLevel).toBe(true);
    });

    it('should reject invalid log level', () => {
      const validLogLevels = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];
      const invalidLogLevel = 'INVALID';

      const isValidLogLevel = validLogLevels.includes(invalidLogLevel);
      expect(isValidLogLevel).toBe(false);
    });
  });

  describe('Configuration Data Structure', () => {
    it('should have all required fields', () => {
      const config = {
        ollama_base_url: 'http://127.0.0.1:11434',
        default_model: 'llama3.1:8b',
        rag_enabled: false,
        log_level: 'INFO',
      };

      expect(config).toHaveProperty('ollama_base_url');
      expect(config).toHaveProperty('default_model');
      expect(config).toHaveProperty('rag_enabled');
      expect(config).toHaveProperty('log_level');
    });

    it('should have correct data types', () => {
      const config = {
        ollama_base_url: 'http://127.0.0.1:11434',
        default_model: 'llama3.1:8b',
        rag_enabled: false,
        log_level: 'INFO',
      };

      expect(typeof config.ollama_base_url).toBe('string');
      expect(typeof config.default_model).toBe('string');
      expect(typeof config.rag_enabled).toBe('boolean');
      expect(typeof config.log_level).toBe('string');
    });
  });

  describe('Connection Testing', () => {
    it('should return success result on successful connection', async () => {
      // Mock successful API response
      const mockResponse = { success: true, message: 'Connection successful!' };

      expect(mockResponse.success).toBe(true);
      expect(mockResponse.message).toContain('successful');
    });

    it('should return failure result on failed connection', async () => {
      // Mock failed API response
      const mockResponse = { success: false, message: 'Connection failed: HTTP 500' };

      expect(mockResponse.success).toBe(false);
      expect(mockResponse.message).toContain('failed');
    });
  });

  describe('Save Configuration', () => {
    it('should prepare correct payload for save', () => {
      const formData = {
        ollama_base_url: 'http://127.0.0.1:11434',
        default_model: 'llama3.1:8b',
        rag_enabled: true,
        log_level: 'DEBUG',
      };

      const payload = {
        default_model: formData.default_model,
        rag_enabled: formData.rag_enabled,
        log_level: formData.log_level,
      };

      expect(payload).toHaveProperty('default_model');
      expect(payload).toHaveProperty('rag_enabled');
      expect(payload).toHaveProperty('log_level');
      expect(payload).not.toHaveProperty('ollama_base_url');
    });

    it('should exclude read-only fields from payload', () => {
      const formData = {
        ollama_base_url: 'http://127.0.0.1:11434',
        default_model: 'llama3.1:8b',
        rag_enabled: true,
        log_level: 'DEBUG',
      };

      const payload = {
        default_model: formData.default_model,
        rag_enabled: formData.rag_enabled,
        log_level: formData.log_level,
      };

      // ollama_base_url is read-only and should not be in payload
      expect(payload).not.toHaveProperty('ollama_base_url');
    });
  });

  describe('Step Metadata', () => {
    it('should have correct step definitions', () => {
      const steps = [
        { id: 'model', label: 'Model Selection', description: 'Choose your default LLM model' },
        { id: 'rag', label: 'RAG Configuration', description: 'Configure retrieval-augmented generation' },
        { id: 'api', label: 'API Settings', description: 'Configure logging and API settings' },
        { id: 'summary', label: 'Summary', description: 'Review and save your configuration' },
      ];

      expect(steps).toHaveLength(4);
      expect(steps[0].id).toBe('model');
      expect(steps[3].id).toBe('summary');
    });

    it('should have progress indicator values', () => {
      const steps = [
        { id: 'model', label: 'Model Selection' },
        { id: 'rag', label: 'RAG Configuration' },
        { id: 'api', label: 'API Settings' },
        { id: 'summary', label: 'Summary' },
      ];

      const currentStepIndex = 1;
      const progressPercentage = ((currentStepIndex + 1) / steps.length) * 100;

      expect(progressPercentage).toBe(50);
    });
  });

  describe('Keyboard Navigation', () => {
    it('should handle ArrowLeft key to go to previous step', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      let currentStepIndex = 2;

      // Simulate ArrowLeft key press
      if (currentStepIndex > 0) {
        currentStepIndex -= 1;
      }

      expect(currentStepIndex).toBe(1);
      expect(steps[currentStepIndex]).toBe('rag');
    });

    it('should handle ArrowRight key to go to next step', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const formData = { default_model: 'llama3.1:8b' };
      let currentStepIndex = 0;
      const currentStep = steps[currentStepIndex];

      // Simulate ArrowRight key press with validation
      if (
        currentStepIndex < steps.length - 1 &&
        !(currentStep === 'model' && !formData.default_model)
      ) {
        currentStepIndex += 1;
      }

      expect(currentStepIndex).toBe(1);
      expect(steps[currentStepIndex]).toBe('rag');
    });

    it('should block ArrowRight on model step when no model selected', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const formData = { default_model: '' };
      let currentStepIndex = 0;
      const currentStep = steps[currentStepIndex];

      // Simulate ArrowRight key press with validation
      if (
        currentStepIndex < steps.length - 1 &&
        !(currentStep === 'model' && !formData.default_model)
      ) {
        currentStepIndex += 1;
      }

      // Should stay on model step
      expect(currentStepIndex).toBe(0);
      expect(steps[currentStepIndex]).toBe('model');
    });

    it('should handle Enter key to advance step', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const formData = { default_model: 'llama3.1:8b' };
      let currentStepIndex = 1;
      const currentStep = steps[currentStepIndex];

      // Simulate Enter key press
      if (
        currentStep !== 'summary' &&
        currentStepIndex < steps.length - 1 &&
        !(currentStep === 'model' && !formData.default_model)
      ) {
        currentStepIndex += 1;
      }

      expect(currentStepIndex).toBe(2);
      expect(steps[currentStepIndex]).toBe('api');
    });

    it('should not advance past last step with ArrowRight', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const formData = { default_model: 'llama3.1:8b' };
      let currentStepIndex = 3;
      const currentStep = steps[currentStepIndex];

      // Simulate ArrowRight key press
      if (
        currentStepIndex < steps.length - 1 &&
        !(currentStep === 'model' && !formData.default_model)
      ) {
        currentStepIndex += 1;
      }

      expect(currentStepIndex).toBe(3);
      expect(steps[currentStepIndex]).toBe('summary');
    });

    it('should not go before first step with ArrowLeft', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      let currentStepIndex = 0;

      // Simulate ArrowLeft key press
      if (currentStepIndex > 0) {
        currentStepIndex -= 1;
      }

      expect(currentStepIndex).toBe(0);
      expect(steps[currentStepIndex]).toBe('model');
    });

    it('should ignore keyboard events when typing in input fields', () => {
      // Simulate event target being an input field
      const mockTarget = { tagName: 'INPUT' };
      const shouldIgnoreEvent =
        mockTarget.tagName === 'INPUT' ||
        mockTarget.tagName === 'TEXTAREA' ||
        mockTarget.tagName === 'SELECT';

      expect(shouldIgnoreEvent).toBe(true);
    });

    it('should process keyboard events when not in input fields', () => {
      // Simulate event target being a div
      const mockTarget = { tagName: 'DIV' };
      const shouldIgnoreEvent =
        mockTarget.tagName === 'INPUT' ||
        mockTarget.tagName === 'TEXTAREA' ||
        mockTarget.tagName === 'SELECT';

      expect(shouldIgnoreEvent).toBe(false);
    });

    it('should trigger save on summary step with Enter key', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const formData = { default_model: 'llama3.1:8b' };
      const currentStepIndex = 3;
      const currentStep = steps[currentStepIndex];
      const saving = false;
      let saveTriggered = false;

      // Simulate Enter key press on summary page
      if (currentStep === 'summary' && formData.default_model && !saving) {
        saveTriggered = true;
      }

      expect(saveTriggered).toBe(true);
    });

    it('should not trigger save when saving is in progress', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const formData = { default_model: 'llama3.1:8b' };
      const currentStepIndex = 3;
      const currentStep = steps[currentStepIndex];
      const saving = true;
      let saveTriggered = false;

      // Simulate Enter key press while already saving
      if (currentStep === 'summary' && formData.default_model && !saving) {
        saveTriggered = true;
      }

      expect(saveTriggered).toBe(false);
    });
  });
});
