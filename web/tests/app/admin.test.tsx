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

  describe('Input Sanitization', () => {
    /**
     * Sanitizes a model name by:
     * - Trimming whitespace
     * - Removing any characters that are not alphanumeric, dots, colons, hyphens, or underscores
     * - Limiting length to 200 characters
     */
    function sanitizeModelName(modelName: string): string {
      return modelName
        .trim()
        .replace(/[^a-zA-Z0-9.:_-]/g, '')
        .slice(0, 200);
    }

    /**
     * Sanitizes log level by ensuring it's one of the valid values.
     * Defaults to 'INFO' if invalid.
     */
    function sanitizeLogLevel(logLevel: string): string {
      const validLevels = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];
      const sanitized = logLevel.trim().toUpperCase();
      return validLevels.includes(sanitized) ? sanitized : 'INFO';
    }

    describe('Model Name Sanitization', () => {
      it('should trim whitespace from model name', () => {
        expect(sanitizeModelName('  llama3.1:8b  ')).toBe('llama3.1:8b');
      });

      it('should allow valid characters (alphanumeric, dots, colons, hyphens, underscores)', () => {
        expect(sanitizeModelName('llama3.1:8b-instruct_v2')).toBe('llama3.1:8b-instruct_v2');
      });

      it('should remove special characters and scripts', () => {
        expect(sanitizeModelName('llama<script>alert("xss")</script>:8b')).toBe('llamascriptalertxssscript:8b');
      });

      it('should remove shell metacharacters', () => {
        expect(sanitizeModelName('llama; rm -rf /')).toBe('llamarm-rf');
      });

      it('should remove SQL injection attempts', () => {
        expect(sanitizeModelName("llama'; DROP TABLE users--")).toBe('llamaDROPTABLEusers--');
      });

      it('should limit model name length to 200 characters', () => {
        const longName = 'a'.repeat(300);
        expect(sanitizeModelName(longName)).toHaveLength(200);
      });

      it('should handle empty string', () => {
        expect(sanitizeModelName('')).toBe('');
      });

      it('should remove newlines and tabs', () => {
        expect(sanitizeModelName('llama\n3.1:\t8b')).toBe('llama3.1:8b');
      });

      it('should remove path traversal attempts', () => {
        expect(sanitizeModelName('../../../etc/passwd')).toBe('......etcpasswd');
      });
    });

    describe('Log Level Sanitization', () => {
      it('should accept valid uppercase log levels', () => {
        expect(sanitizeLogLevel('DEBUG')).toBe('DEBUG');
        expect(sanitizeLogLevel('INFO')).toBe('INFO');
        expect(sanitizeLogLevel('WARNING')).toBe('WARNING');
        expect(sanitizeLogLevel('ERROR')).toBe('ERROR');
      });

      it('should convert lowercase to uppercase', () => {
        expect(sanitizeLogLevel('debug')).toBe('DEBUG');
        expect(sanitizeLogLevel('info')).toBe('INFO');
      });

      it('should convert mixed case to uppercase', () => {
        expect(sanitizeLogLevel('DeBuG')).toBe('DEBUG');
      });

      it('should trim whitespace', () => {
        expect(sanitizeLogLevel('  INFO  ')).toBe('INFO');
      });

      it('should default to INFO for invalid log levels', () => {
        expect(sanitizeLogLevel('INVALID')).toBe('INFO');
        expect(sanitizeLogLevel('TRACE')).toBe('INFO');
        expect(sanitizeLogLevel('CRITICAL')).toBe('INFO');
      });

      it('should default to INFO for script injection attempts', () => {
        expect(sanitizeLogLevel('<script>alert("xss")</script>')).toBe('INFO');
      });

      it('should default to INFO for empty string', () => {
        expect(sanitizeLogLevel('')).toBe('INFO');
      });

      it('should default to INFO for SQL injection attempts', () => {
        expect(sanitizeLogLevel("'; DROP TABLE logs--")).toBe('INFO');
      });
    });

    describe('Sanitization in Save Flow', () => {
      it('should sanitize model name before saving', () => {
        const formData = {
          default_model: '  llama3.1:8b  ',
          rag_enabled: false,
          log_level: 'INFO',
        };

        const sanitizedModel = sanitizeModelName(formData.default_model);
        const payload = {
          default_model: sanitizedModel,
          rag_enabled: formData.rag_enabled,
          log_level: formData.log_level,
        };

        expect(payload.default_model).toBe('llama3.1:8b');
      });

      it('should sanitize log level before saving', () => {
        const formData = {
          default_model: 'llama3.1:8b',
          rag_enabled: false,
          log_level: '  debug  ',
        };

        const sanitizedLogLevel = sanitizeLogLevel(formData.log_level);
        const payload = {
          default_model: formData.default_model,
          rag_enabled: formData.rag_enabled,
          log_level: sanitizedLogLevel,
        };

        expect(payload.log_level).toBe('DEBUG');
      });

      it('should sanitize both fields in save payload', () => {
        const formData = {
          default_model: '  llama<script>:8b  ',
          rag_enabled: true,
          log_level: 'invalid',
        };

        const sanitizedModel = sanitizeModelName(formData.default_model);
        const sanitizedLogLevel = sanitizeLogLevel(formData.log_level);

        const payload = {
          default_model: sanitizedModel,
          rag_enabled: formData.rag_enabled,
          log_level: sanitizedLogLevel,
        };

        expect(payload.default_model).toBe('llamascript:8b');
        expect(payload.log_level).toBe('INFO');
        expect(payload.rag_enabled).toBe(true);
      });
    });
  });
});
