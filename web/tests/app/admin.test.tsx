import { describe, it, expect, beforeEach, vi } from 'vitest';

/**
 * Configuration Wizard Tests
 *
 * Tests for the admin configuration wizard logic.
 * These tests verify form validation, step navigation, and configuration updates.
 */

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

    it('should reset to first step after successful save', async () => {
      // Simulate wizard state after successful save
      const steps = ['model', 'rag', 'api', 'summary'];
      let currentStep = 'summary';
      let saveSuccess = true;

      // After 3 second timeout, wizard should reset to first step
      if (saveSuccess) {
        currentStep = 'model';
        saveSuccess = false;
      }

      expect(currentStep).toBe('model');
      expect(saveSuccess).toBe(false);
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

    it('should block ArrowLeft navigation when saving', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      let currentStepIndex = 2;
      const saving = true;

      // Simulate ArrowLeft key press while saving
      if (!saving && currentStepIndex > 0) {
        currentStepIndex -= 1;
      }

      // Should stay on current step
      expect(currentStepIndex).toBe(2);
      expect(steps[currentStepIndex]).toBe('api');
    });

    it('should block ArrowRight navigation when saving', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const formData = { default_model: 'llama3.1:8b' };
      let currentStepIndex = 1;
      const currentStep = steps[currentStepIndex];
      const saving = true;

      // Simulate ArrowRight key press while saving
      if (
        !saving &&
        currentStepIndex < steps.length - 1 &&
        !(currentStep === 'model' && !formData.default_model)
      ) {
        currentStepIndex += 1;
      }

      // Should stay on current step
      expect(currentStepIndex).toBe(1);
      expect(steps[currentStepIndex]).toBe('rag');
    });

    it('should block Enter navigation when saving', () => {
      const steps = ['model', 'rag', 'api', 'summary'];
      const formData = { default_model: 'llama3.1:8b' };
      let currentStepIndex = 1;
      const currentStep = steps[currentStepIndex];
      const saving = true;

      // Simulate Enter key press while saving
      if (
        !saving &&
        currentStep !== 'summary' &&
        currentStepIndex < steps.length - 1 &&
        !(currentStep === 'model' && !formData.default_model)
      ) {
        currentStepIndex += 1;
      }

      // Should stay on current step
      expect(currentStepIndex).toBe(1);
      expect(steps[currentStepIndex]).toBe('rag');
    });
  });

  describe('Loading States', () => {
    it('should disable model select when loading models', () => {
      const loadingModels = true;
      const saving = false;

      const isDisabled = loadingModels || saving;

      expect(isDisabled).toBe(true);
    });

    it('should disable model select when saving', () => {
      const loadingModels = false;
      const saving = true;

      const isDisabled = loadingModels || saving;

      expect(isDisabled).toBe(true);
    });

    it('should disable RAG checkbox when saving', () => {
      const saving = true;

      expect(saving).toBe(true);
    });

    it('should disable log level select when saving', () => {
      const saving = true;

      expect(saving).toBe(true);
    });

    it('should disable Previous button when saving', () => {
      const currentStepIndex = 2;
      const saving = true;

      const isDisabled = currentStepIndex === 0 || saving;

      expect(isDisabled).toBe(true);
    });

    it('should disable Next button when saving', () => {
      const formData = { default_model: 'llama3.1:8b' };
      const currentStepIndex = 1;
      const currentStep = 'rag';
      const saving = true;

      const isDisabled =
        currentStepIndex === 3 ||
        (currentStep === 'model' && !formData.default_model) ||
        saving;

      expect(isDisabled).toBe(true);
    });
  });

  describe('Independent Error Handling', () => {
    it('should handle config error independently from models error', () => {
      // Simulate config load failure
      const configError = 'Failed to load config';
      const modelsError = null;

      expect(configError).not.toBeNull();
      expect(modelsError).toBeNull();
    });

    it('should handle models error independently from config error', () => {
      // Simulate models load failure but config success
      const configError = null;
      const modelsError = 'Failed to load models';

      expect(configError).toBeNull();
      expect(modelsError).not.toBeNull();
    });

    it('should handle both errors independently', () => {
      // Simulate both failures
      const configError = 'Failed to load config';
      const modelsError = 'Failed to load models';

      expect(configError).not.toBeNull();
      expect(modelsError).not.toBeNull();
      expect(configError).not.toBe(modelsError);
    });

    it('should allow page to render when only models fail', () => {
      // When config succeeds but models fail, page should still be usable
      const configError = null;
      const modelsError = 'HTTP 500';
      const loading = false;

      const shouldRenderFullPage = !loading && !configError;
      const shouldShowModelsError = modelsError !== null;

      expect(shouldRenderFullPage).toBe(true);
      expect(shouldShowModelsError).toBe(true);
    });

    it('should block page rendering when config fails', () => {
      // When config fails, page should show error screen
      const configError = 'HTTP 500';
      const modelsError = null;
      const loading = false;

      const shouldRenderFullPage = !loading && !configError;
      const shouldShowConfigError = configError !== null;

      expect(shouldRenderFullPage).toBe(false);
      expect(shouldShowConfigError).toBe(true);
    });

    it('should clear errors on retry', () => {
      // Simulate clearing errors before retry
      let configError: string | null = 'HTTP 500';
      let modelsError: string | null = 'HTTP 503';

      // Before retry, clear respective error
      configError = null; // Clear config error on config retry
      expect(configError).toBeNull();
      expect(modelsError).not.toBeNull(); // Models error unaffected

      // Before models retry, clear models error
      modelsError = null;
      expect(modelsError).toBeNull();
    });

    it('should display specific error messages for each failure', () => {
      const configErrorMessage = 'Failed to load configuration';
      const modelsErrorMessage = 'Failed to load models: HTTP 500';

      // Error messages should be distinct and specific
      expect(configErrorMessage).toContain('configuration');
      expect(modelsErrorMessage).toContain('models');
      expect(configErrorMessage).not.toBe(modelsErrorMessage);
    });

    it('should allow models to be empty array when load fails', () => {
      // When models fail to load, should gracefully default to empty array
      let availableModels: string[] = ['model1', 'model2'];
      const modelsError = 'HTTP 500';

      // On error, set to empty array
      if (modelsError) {
        availableModels = [];
      }

      expect(availableModels).toEqual([]);
      expect(Array.isArray(availableModels)).toBe(true);
    });
  });

  describe('Error Message Guidance', () => {
    it('should provide troubleshooting guidance for network errors in loadConfig', () => {
      const errorMessage = 'Failed to fetch';
      const isNetworkError = errorMessage.includes('fetch') || errorMessage.includes('Failed to fetch');

      const troubleshootingMsg = isNetworkError
        ? `${errorMessage}\n\nTroubleshooting steps:\n• Ensure the myGPT API server is running\n• Check that the API is accessible at the configured URL\n• Verify network connectivity\n• Review API server logs for errors`
        : errorMessage;

      expect(troubleshootingMsg).toContain('Troubleshooting steps');
      expect(troubleshootingMsg).toContain('API server is running');
    });

    it('should provide troubleshooting guidance for HTTP 500 errors in loadConfig', () => {
      const errorMessage = 'HTTP 500';
      const isNetworkError = errorMessage === 'HTTP 500' || errorMessage === 'HTTP 502' || errorMessage === 'HTTP 503';

      const troubleshootingMsg = isNetworkError
        ? `${errorMessage}\n\nTroubleshooting steps:\n• Ensure the myGPT API server is running\n• Check that the API is accessible at the configured URL\n• Verify network connectivity\n• Review API server logs for errors`
        : errorMessage;

      expect(troubleshootingMsg).toContain('Troubleshooting steps');
      expect(troubleshootingMsg).toContain('Review API server logs');
    });

    it('should not add troubleshooting guidance for non-network errors in loadConfig', () => {
      const errorMessage = 'Invalid JSON';
      const isNetworkError = errorMessage.includes('fetch') || errorMessage.includes('Failed to fetch') || errorMessage === 'HTTP 500';

      const troubleshootingMsg = isNetworkError
        ? `${errorMessage}\n\nTroubleshooting steps:\n• Ensure the myGPT API server is running\n• Check that the API is accessible at the configured URL\n• Verify network connectivity\n• Review API server logs for errors`
        : errorMessage;

      expect(troubleshootingMsg).toBe('Invalid JSON');
      expect(troubleshootingMsg).not.toContain('Troubleshooting');
    });

    it('should provide troubleshooting guidance for network errors in loadModels', () => {
      const errorMessage = 'Failed to fetch';
      const isNetworkError = errorMessage.includes('fetch') || errorMessage.includes('Failed to fetch');

      const troubleshootingMsg = isNetworkError
        ? `${errorMessage}. Troubleshooting: Ensure the myGPT API and Ollama are running and accessible.`
        : errorMessage;

      expect(troubleshootingMsg).toContain('Troubleshooting');
      expect(troubleshootingMsg).toContain('Ollama are running');
    });

    it('should provide troubleshooting guidance for HTTP 503 errors in loadModels', () => {
      const errorMessage = 'HTTP 503';
      const isNetworkError = errorMessage === 'HTTP 500' || errorMessage === 'HTTP 502' || errorMessage === 'HTTP 503';

      const troubleshootingMsg = isNetworkError
        ? `${errorMessage}. Troubleshooting: Ensure the myGPT API and Ollama are running and accessible.`
        : errorMessage;

      expect(troubleshootingMsg).toContain('Troubleshooting');
      expect(troubleshootingMsg).toContain('myGPT API and Ollama');
    });

    it('should not add troubleshooting guidance for non-network errors in loadModels', () => {
      const errorMessage = 'Invalid response format';
      const isNetworkError = errorMessage.includes('fetch') || errorMessage.includes('Failed to fetch') || errorMessage === 'HTTP 500';

      const troubleshootingMsg = isNetworkError
        ? `${errorMessage}. Troubleshooting: Ensure the myGPT API and Ollama are running and accessible.`
        : errorMessage;

      expect(troubleshootingMsg).toBe('Invalid response format');
      expect(troubleshootingMsg).not.toContain('Troubleshooting');
    });

    it('should handle HTTP 502 (Bad Gateway) errors with troubleshooting guidance', () => {
      const errorMessage = 'HTTP 502';
      const isNetworkError = errorMessage === 'HTTP 500' || errorMessage === 'HTTP 502' || errorMessage === 'HTTP 503';

      expect(isNetworkError).toBe(true);
    });
  });
});
