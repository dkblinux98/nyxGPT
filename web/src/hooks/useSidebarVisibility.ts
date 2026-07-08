'use client';

import { Dispatch, SetStateAction, useCallback, useState } from 'react';

/**
 * Sidebar visibility with a mobile-aware default.
 *
 * Until the user explicitly shows/hides the sidebar, visibility is derived
 * from `isMobile` (hidden on mobile, shown on desktop) so it collapses as
 * soon as a mobile viewport is detected. This is computed in the same render
 * as the `isMobile` update rather than via a follow-up effect, which avoids a
 * flash of the full-screen overlay before an effect could hide it. Once the
 * user makes an explicit choice, that choice sticks regardless of further
 * `isMobile` changes.
 */
export function useSidebarVisibility(isMobile: boolean): [boolean, Dispatch<SetStateAction<boolean>>] {
  const [override, setOverride] = useState<boolean | null>(null);
  const sidebarVisible = override ?? !isMobile;

  const setSidebarVisible: Dispatch<SetStateAction<boolean>> = useCallback(
    (value) => {
      setOverride((prevOverride) => {
        const prevVisible = prevOverride ?? !isMobile;
        return typeof value === 'function' ? (value as (prev: boolean) => boolean)(prevVisible) : value;
      });
    },
    [isMobile]
  );

  return [sidebarVisible, setSidebarVisible];
}
