import { describe, it, expect } from 'vitest';

describe('Test Infrastructure', () => {
  it('vitest is working', () => {
    expect(true).toBe(true);
  });

  it('can perform basic assertions', () => {
    const value = 42;
    expect(value).toBe(42);
    expect(value).toBeGreaterThan(40);
    expect(value).toBeLessThan(50);
  });

  it('can test async code', async () => {
    const promise = Promise.resolve('test');
    await expect(promise).resolves.toBe('test');
  });

  it('mocks are available', () => {
    const mockFn = vi.fn();
    mockFn('test');
    expect(mockFn).toHaveBeenCalledWith('test');
  });
});
