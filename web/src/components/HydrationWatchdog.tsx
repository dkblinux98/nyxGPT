import { hydrationWatchdogScript } from '../lib/hydrationWatchdog';

/**
 * Emits the document-inline hydration watchdog (#3857).
 *
 * Deliberately **not** a client component and deliberately not `next/script`:
 * the whole point is that this code arrives inside the HTML document and runs
 * without the client bundle, because the failure it reports is the client
 * bundle never arriving. Anything that resolves through
 * `/_next/static/chunks/` cannot report that chunk pipeline being broken.
 *
 * Renders one `<script>` whose body is a fixed string (see
 * `hydrationWatchdogScript`), so server and client markup are identical and
 * hydration has nothing to mismatch on.
 */
export default function HydrationWatchdog() {
  return <script dangerouslySetInnerHTML={{ __html: hydrationWatchdogScript() }} />;
}
