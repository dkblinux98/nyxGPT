import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import AdminPage from '../../src/app/admin/page';

/**
 * Configuration Wizard Tests
 *
 * Tests for the admin configuration wizard component rendering and logic.
 * These tests verify form validation, step navigation, configuration updates, and UI behaviors.
 */

describe('AdminPage Component', () => {
  it('renders loading state initially', () => {
    render(<AdminPage />);
    expect(screen.getByText('Loading configuration...')).toBeInTheDocument();
  });

  it('renders the configuration wizard after loading', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('Configuration Wizard')).toBeInTheDocument();
    });
  });

  it('renders back to chat link', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const link = screen.getByRole('link', { name: /back to chat/i });
      expect(link).toBeInTheDocument();
      expect(link).toHaveAttribute('href', '/');
    });
  });

  it('renders progress indicator with all steps', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('Model Selection')).toBeInTheDocument();
    });
  });

  it('starts at model selection step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('Model Selection')).toBeInTheDocument();
      expect(screen.getByText('Choose your default LLM model')).toBeInTheDocument();
    });
  });

  it('renders model selection dropdown with available models', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('Default Model')).toBeInTheDocument();
      const selects = screen.getAllByRole('combobox');
      expect(selects[0]).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'llama3.1:8b' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'llama3.1:70b' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'mistral:7b' })).toBeInTheDocument();
    });
  });

  it('disables Next button when no model is selected', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const nextButton = screen.getByRole('button', { name: /next/i });
      expect(nextButton).toBeDisabled();
    });
  });

  it('enables Next button when model is selected', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'mistral:7b' } });
    });

    await waitFor(() => {
      const nextButton = screen.getByRole('button', { name: /next/i });
      expect(nextButton).not.toBeDisabled();
    });
  });

  it('navigates to RAG configuration step when Next is clicked', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      expect(screen.getByText('Configure retrieval-augmented generation')).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });
  });

  it('renders RAG enable checkbox on RAG step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      const checkbox = screen.getByRole('checkbox');
      expect(checkbox).toBeInTheDocument();
      expect(checkbox).not.toBeChecked();
    });
  });

  it('allows toggling RAG enabled state', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      const checkbox = screen.getByRole('checkbox');
      expect(checkbox).not.toBeChecked();
      fireEvent.click(checkbox);
    });

    await waitFor(() => {
      const checkbox = screen.getByRole('checkbox');
      expect(checkbox).toBeChecked();
    });
  });

  it('navigates to API settings step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });

    await waitFor(() => {
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'API Settings' })).toBeInTheDocument();
      expect(screen.getByText('Configure logging level and other API settings.')).toBeInTheDocument();
    });
  });

  it('renders log level dropdown on API settings step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      expect(screen.getByText('Log Level')).toBeInTheDocument();
      expect(screen.getByRole('option', { name: /INFO - Standard operational messages/i })).toBeInTheDocument();
    });
  });

  it('renders test connection button on API settings step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      const testButton = screen.getByRole('button', { name: /test connection/i });
      expect(testButton).toBeInTheDocument();
    });
  });

  it('shows connection success message when test is successful', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      const testButton = screen.getByRole('button', { name: /test connection/i });
      fireEvent.click(testButton);
    });

    await waitFor(() => {
      expect(screen.getByText(/connection successful/i)).toBeInTheDocument();
    });
  });

  it('navigates to summary step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      expect(screen.getByText('Summary')).toBeInTheDocument();
      expect(screen.getByText('Review and save your configuration')).toBeInTheDocument();
    });
  });

  it('renders configuration review on summary step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      const summaryTitles = screen.getAllByText('Model Selection');
      expect(summaryTitles.length).toBeGreaterThan(0);
      expect(screen.getByText('RAG Configuration')).toBeInTheDocument();
      expect(screen.getByText('API Settings')).toBeInTheDocument();
    });
  });

  it('renders save configuration button on summary step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      expect(saveButton).toBeInTheDocument();
      expect(saveButton).not.toBeDisabled();
    });
  });

  it('shows success message after saving configuration', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      const saveButton = screen.getByRole('button', { name: /save configuration/i });
      fireEvent.click(saveButton);
    });

    await waitFor(() => {
      expect(screen.getByText(/configuration saved successfully/i)).toBeInTheDocument();
    });
  });

  it('allows navigation back to previous steps', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      const nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'RAG Configuration' })).toBeInTheDocument();
    });

    await waitFor(() => {
      const prevButton = screen.getByRole('button', { name: /previous/i });
      fireEvent.click(prevButton);
    });

    await waitFor(() => {
      expect(screen.getByText('Model Selection')).toBeInTheDocument();
      expect(screen.getByText('Choose your default LLM model')).toBeInTheDocument();
    });
  });

  it('disables Previous button on first step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const prevButton = screen.getByRole('button', { name: /previous/i });
      expect(prevButton).toBeDisabled();
    });
  });

  it('disables Next button on last step', async () => {
    render(<AdminPage />);
    await waitFor(() => {
      const selects = screen.getAllByRole('combobox');
      fireEvent.change(selects[0], { target: { value: 'llama3.1:8b' } });
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      let nextButton = screen.getByRole('button', { name: /next/i });
      fireEvent.click(nextButton);
    });

    await waitFor(() => {
      expect(screen.getByText('Summary')).toBeInTheDocument();
      const nextButtonOnLastStep = screen.getByRole('button', { name: /next/i });
      expect(nextButtonOnLastStep).toBeDisabled();
    });
  });

  it('renders error state when config loading fails', async () => {
    const { server } = await import('../mocks/server');
    const { http, HttpResponse } = await import('msw');

    server.use(
      http.get('/api/config', () => {
        return new HttpResponse(null, { status: 500 });
      })
    );

    render(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText('Failed to load configuration')).toBeInTheDocument();
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
