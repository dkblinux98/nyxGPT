/**
 * hydrationWatchdog tests (#3857)
 *
 * The failure this covers is the one the incident actually recorded: the
 * client bundle never runs, so nothing React-side -- no effect, no error
 * boundary, no chunk timeout -- ever executes, and the server-rendered
 * loading placeholders stay on screen forever. The only thing that can report
 * that state is code delivered inside the document, so these tests execute the
 * inline script exactly as the browser would (`new Function(source)()`) with
 * no bundle present at all.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  HYDRATION_FLAG,
  HYDRATION_WATCHDOG_ELEMENT_ID,
  HYDRATION_WATCHDOG_TIMEOUT_MS,
  hydrationWatchdogScript,
  markHydrated,
} from '../../src/lib/hydrationWatchdog';

type Mutable = Record<string, unknown>;

/** Run the inline script the way the browser does: as a bare source string. */
function runInlineScript(source: string = hydrationWatchdogScript()): void {
  new Function(source)();
}

function surface(): HTMLElement | null {
  return document.getElementById(HYDRATION_WATCHDOG_ELEMENT_ID);
}

let unregistered: number;
let deletedCaches: string[];

function installBrowserDoubles(options: { controller?: unknown } = {}) {
  Object.defineProperty(globalThis, 'caches', {
    configurable: true,
    writable: true,
    value: {
      keys: vi.fn(async () => ['static-resources', 'workbox-precache-v2-x']),
      delete: vi.fn(async (name: string) => {
        deletedCaches.push(name);
        return true;
      }),
    },
  });
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    writable: true,
    value: {
      controller: 'controller' in options ? options.controller : null,
      getRegistrations: vi.fn(async () => [
        {
          unregister: vi.fn(async () => {
            unregistered += 1;
            return true;
          }),
        },
      ]),
    },
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  unregistered = 0;
  deletedCaches = [];
  document.body.innerHTML = '';
  delete (window as unknown as Mutable)[HYDRATION_FLAG];
  delete (window as unknown as Mutable).__nyxgptWatchdogArmed;
  installBrowserDoubles();
});

afterEach(() => {
  vi.useRealTimers();
  // @ts-expect-error - remove the doubles again
  delete globalThis.caches;
  // @ts-expect-error - deleting an optional browser API for this test
  delete navigator.serviceWorker;
  vi.restoreAllMocks();
});

describe('hydrationWatchdogScript', () => {
  it('paints a failure surface when hydration never happens', () => {
    // The exact incident: server-rendered placeholders on screen, no client JS.
    document.body.innerHTML = '<div aria-label="Loading sessions">skeleton</div>';
    runInlineScript();

    expect(surface()).toBeNull();
    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS);

    const alert = surface();
    expect(alert).not.toBeNull();
    expect(alert!.getAttribute('role')).toBe('alert');
    expect(alert!.textContent).toContain('Failed to load the interface');
    // The skeleton is still in the DOM; the surface covers it rather than
    // pretending the underlying page recovered.
    expect(document.querySelector('[aria-label="Loading sessions"]')).not.toBeNull();
  });

  it('stays silent when hydration announced itself in time', () => {
    runInlineScript();
    markHydrated();

    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS * 2);

    expect(surface()).toBeNull();
  });

  it('does not paint before the timeout elapses', () => {
    runInlineScript();
    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS - 1);
    expect(surface()).toBeNull();
  });

  it('arms only once even if the script is evaluated twice', () => {
    runInlineScript();
    runInlineScript();

    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS);

    expect(document.querySelectorAll(`#${HYDRATION_WATCHDOG_ELEMENT_ID}`)).toHaveLength(1);
  });

  it('honours a custom timeout and reports it in the details', () => {
    runInlineScript(hydrationWatchdogScript(500));

    vi.advanceTimersByTime(500);

    expect(surface()!.textContent).toContain('did not run within 500ms');
  });

  it('names a controlling service worker as the discriminator #3857 asks for', () => {
    installBrowserDoubles({ controller: {} });
    runInlineScript();

    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS);

    expect(surface()!.textContent).toContain('A service worker is controlling this page');
  });

  it('says so when no service worker is controlling the page', () => {
    runInlineScript();

    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS);

    expect(surface()!.textContent).toContain('No service worker is controlling this page');
  });

  it('unregisters workers and clears caches before reloading', async () => {
    const reload = vi.fn();
    const originalLocation = window.location;
    // @ts-expect-error - stub window.location.reload for this test
    delete window.location;
    // @ts-expect-error - partial Location stub
    window.location = { ...originalLocation, reload };

    runInlineScript();
    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS);
    const button = surface()!.querySelector('button')!;
    button.click();

    expect(button.textContent).toBe('Reloading...');
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    expect(unregistered).toBe(1);
    expect(deletedCaches).toEqual(['static-resources', 'workbox-precache-v2-x']);

    // @ts-expect-error - restore the real location
    delete window.location;
    // @ts-expect-error - restore the real location
    window.location = originalLocation;
  });

  it('still reloads in a browser with neither service workers nor Cache Storage', async () => {
    // @ts-expect-error - deleting an optional browser API for this test
    delete navigator.serviceWorker;
    // @ts-expect-error - deleting an optional browser API for this test
    delete globalThis.caches;

    const reload = vi.fn();
    const originalLocation = window.location;
    // @ts-expect-error - stub window.location.reload for this test
    delete window.location;
    // @ts-expect-error - partial Location stub
    window.location = { ...originalLocation, reload };

    runInlineScript();
    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS);
    surface()!.querySelector('button')!.click();

    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));

    // @ts-expect-error - restore the real location
    delete window.location;
    // @ts-expect-error - restore the real location
    window.location = originalLocation;
  });

  it('interpolates nothing but a number into the source', () => {
    // The script is inlined into the document, so it must not be a hole
    // anything can be injected through.
    expect(hydrationWatchdogScript(1234)).toContain('MS=1234');
    expect(hydrationWatchdogScript()).not.toContain('</script');
  });
});

describe('markHydrated', () => {
  it('sets the flag the inline script waits on', () => {
    markHydrated();
    expect((window as unknown as Mutable)[HYDRATION_FLAG]).toBe(true);
  });

  it('dismisses a surface that was painted before a slow client hydrated', () => {
    runInlineScript();
    vi.advanceTimersByTime(HYDRATION_WATCHDOG_TIMEOUT_MS);
    expect(surface()).not.toBeNull();

    markHydrated();

    expect(surface()).toBeNull();
  });

  it('does nothing outside a browser', () => {
    const realWindow = globalThis.window;
    // @ts-expect-error - simulate server rendering, where there is no window
    delete globalThis.window;

    expect(() => markHydrated()).not.toThrow();

    globalThis.window = realWindow;
  });
});
