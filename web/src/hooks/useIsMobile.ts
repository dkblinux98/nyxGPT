'use client';

import { useEffect, useState } from 'react';

export const MOBILE_BREAKPOINT_PX = 768;

/**
 * useIsMobile – tracks whether the viewport is at or below the mobile
 * breakpoint (default 768px).
 *
 * Starts as `false` so the first client render matches the server-rendered
 * (desktop) markup; the real value is applied via `matchMedia` immediately
 * after mount and kept in sync as the viewport is resized or rotated.
 */
export function useIsMobile(breakpointPx: number = MOBILE_BREAKPOINT_PX): boolean {
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    const mql = window.matchMedia(`(max-width: ${breakpointPx - 1}px)`);
    const update = () => setIsMobile(mql.matches);

    update();
    mql.addEventListener('change', update);
    return () => mql.removeEventListener('change', update);
  }, [breakpointPx]);

  return isMobile;
}
