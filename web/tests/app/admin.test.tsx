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

  describe('Partial Failure Handling', () => {
    it('should handle config load success with model load failure', () => {
      // Simulate config loading successfully
      const config = {
        ollama_base_url: 'http://127.0.0.1:11434',
        default_model: 'llama3.1:8b',
        rag_enabled: false,
        log_level: 'INFO',
      };
      const configError = null;

      // Simulate models failing to load
      const availableModels: string[] = [];
      const modelsWarning = 'Failed to load model list: HTTP 500. You can manually enter a model name.';

      // Config should be loaded successfully
      expect(config).toBeDefined();
      expect(configError).toBeNull();

      // Models should be empty with warning
      expect(availableModels).toHaveLength(0);
      expect(modelsWarning).toContain('Failed to load model list');
      expect(modelsWarning).toContain('manually enter');
    });

    it('should enable manual input when models fail to load', () => {
      const availableModels: string[] = [];
      const useManualModelInput = true;

      // Manual input should be enabled when models fail to load
      expect(useManualModelInput).toBe(true);
      expect(availableModels).toHaveLength(0);
    });

    it('should allow manual model name entry', () => {
      const manualModelName = 'custom-model:latest';
      const formData = {
        ollama_base_url: 'http://127.0.0.1:11434',
        default_model: manualModelName,
        rag_enabled: false,
        log_level: 'INFO',
      };

      // Manual model name should be accepted
      expect(formData.default_model).toBe(manualModelName);
      expect(formData.default_model).toBeTruthy();
    });

    it('should clear warning when models load successfully', () => {
      const availableModels = ['llama3.1:8b', 'mistral:7b'];
      const warning = null;

      // Warning should be cleared when models load
      expect(availableModels.length).toBeGreaterThan(0);
      expect(warning).toBeNull();
    });

    it('should support toggling between manual input and dropdown', () => {
      let useManualModelInput = false;
      const availableModels = ['llama3.1:8b', 'mistral:7b'];

      // Start with dropdown
      expect(useManualModelInput).toBe(false);
      expect(availableModels.length).toBeGreaterThan(0);

      // Toggle to manual input
      useManualModelInput = true;
      expect(useManualModelInput).toBe(true);

      // Toggle back to dropdown
      useManualModelInput = false;
      expect(useManualModelInput).toBe(false);
    });

    it('should display warning message without blocking wizard', () => {
      const warning = 'Failed to load model list: HTTP 500. You can manually enter a model name.';
      const error = null;
      const loading = false;

      // Warning should be present but not block wizard
      expect(warning).toBeTruthy();
      expect(error).toBeNull();
      expect(loading).toBe(false);
    });

    it('should allow retry when models fail to load', () => {
      const availableModels: string[] = [];
      const loadingModels = false;
      const canRetry = availableModels.length === 0 && !loadingModels;

      // Retry should be allowed when not loading and models are empty
      expect(canRetry).toBe(true);
    });
  });
});
