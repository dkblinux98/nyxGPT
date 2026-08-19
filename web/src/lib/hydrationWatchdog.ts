/**
 * A failure surface that does not depend on the client bundle (#3857).
 *
 * `withChunkTimeout` and `ChunkErrorBoundary` only help once the client
 * bootstrap has run: they are themselves shipped in `/_next/static/chunks/`,
 * the very pipeline whose failure they exist to report. The incident this
 * issue records is the case where that pipeline delivers nothing at all --
 * `webpack-*.js` / `main-app-*.js` never execute, so hydration never happens,
 * no effect fires, no boundary mounts. What stays on screen is the
 * *server-rendered* HTML: the `loading:` fallbacks of the `ssr: false` dynamic
 * imports on `/`, and `Loading canary status...` on
 * `src/app/admin/canary/page.tsx` (a plain `useState(true)` that only clears
 * in an effect). Both render forever, and every client-side guard is asleep.
 *
 * The only code that is guaranteed to run in that state is code that arrives
 * *with the document*. So this module emits an inline `<script>` -- no import,
 * no chunk, no module graph -- that arms a timer at parse time and, if
 * hydration has still not announced itself when it fires, paints the same
 * "Failed to load the interface" surface with the same service-worker-clearing
 * reload, in plain DOM calls.
 *
 * The handshake is one flag:
 *   - the inline script waits for `window.__nyxgptHydrated`;
 *   - `markHydrated()` sets it from a client effect (`HydrationMarker`, mounted
 *     in the root layout), which by definition only runs if the bundle loaded
 *     and React hydrated.
 *
 * Hydration that arrives *after* the watchdog has painted is handled too:
 * `markHydrated` removes the surface, so a merely slow client is never left
 * with an error over a working page.
 */

/** Global flag set once React has hydrated; read by the inline script. */
export const HYDRATION_FLAG = '__nyxgptHydrated';

/** `id` of the element the inline script paints, so it can be de-duplicated. */
export const HYDRATION_WATCHDOG_ELEMENT_ID = 'nyxgpt-hydration-failure';

/**
 * How long the document waits for hydration before declaring the client dead.
 *
 * Matched to `CHUNK_LOAD_TIMEOUT_MS`: the two bound the same user-visible
 * wait, one for a single chunk and one for the whole bootstrap, and they
 * should not disagree about how long "still loading" is allowed to look like
 * "working".
 */
export const HYDRATION_WATCHDOG_TIMEOUT_MS = 20_000;

declare global {
  interface Window {
    [HYDRATION_FLAG]?: boolean;
  }
}

/**
 * Announce that the client bundle loaded and React hydrated, disarming the
 * document-inline watchdog (and dismissing its surface if it already fired).
 */
export function markHydrated(): void {
  if (typeof window === 'undefined') return;
  window[HYDRATION_FLAG] = true;
  const painted = document.getElementById(HYDRATION_WATCHDOG_ELEMENT_ID);
  if (painted) painted.remove();
}

/**
 * The inline script's source.
 *
 * Deliberately plain, dependency-free ES5: it runs before (and possibly
 * instead of) anything the bundler produced, so it cannot use a helper, a
 * polyfill, or a framework API. `timeoutMs` is the only interpolated value and
 * is coerced to a number, so nothing here can be injected into.
 */
export function hydrationWatchdogScript(
  timeoutMs: number = HYDRATION_WATCHDOG_TIMEOUT_MS,
): string {
  return `(function(){
var FLAG=${JSON.stringify(HYDRATION_FLAG)},ID=${JSON.stringify(HYDRATION_WATCHDOG_ELEMENT_ID)},MS=${Number(timeoutMs)};
if(typeof window==="undefined")return;
if(window.__nyxgptWatchdogArmed)return;
window.__nyxgptWatchdogArmed=true;
function purge(done){
var jobs=[];
try{
if(navigator.serviceWorker&&navigator.serviceWorker.getRegistrations){
jobs.push(navigator.serviceWorker.getRegistrations().then(function(rs){
return Promise.all(rs.map(function(r){return r.unregister()}))}))}
if(window.caches&&caches.keys){
jobs.push(caches.keys().then(function(ks){
return Promise.all(ks.map(function(k){return caches.delete(k)}))}))}
}catch(e){}
if(!jobs.length){done();return}
Promise.all(jobs).then(done,done)}
function el(tag,css,text){var n=document.createElement(tag);n.style.cssText=css;if(text)n.textContent=text;return n}
function paint(){
if(window[FLAG])return;
if(document.getElementById(ID))return;
if(!document.body)return;
var sw=!!(navigator.serviceWorker&&navigator.serviceWorker.controller);
var box=el("div","position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;padding:1.5rem;background:rgba(0,0,0,0.72)");
box.id=ID;box.setAttribute("role","alert");
var card=el("div","max-width:26rem;width:100%;background:#fff;color:#111;border-radius:10px;padding:1.5rem;text-align:center;font-family:system-ui,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,0.35)");
card.appendChild(el("div","font-size:24px","\\u26A0\\uFE0F"));
card.appendChild(el("div","font-weight:600;margin-top:8px","Failed to load the interface"));
card.appendChild(el("div","font-size:14px;opacity:0.85;margin-top:8px","The app's code did not finish loading, so this page is showing placeholders that will never fill in. The server is not necessarily down -- this usually means the browser is holding an outdated copy of the app, which reloading clears."));
var btn=document.createElement("button");
btn.type="button";btn.textContent="Reload";
btn.style.cssText="margin-top:14px;padding:8px 16px;border-radius:6px;border:none;font-weight:600;font-size:14px;cursor:pointer";
btn.onclick=function(){btn.disabled=true;btn.textContent="Reloading...";purge(function(){window.location.reload()})};
card.appendChild(btn);
var det=document.createElement("details");
det.style.cssText="font-size:12px;opacity:0.7;margin-top:14px;text-align:left";
var sum=document.createElement("summary");sum.style.cssText="cursor:pointer";sum.textContent="Details";
det.appendChild(sum);
det.appendChild(el("div","margin-top:6px","The client bundle did not run within "+MS+"ms (no hydration)."));
det.appendChild(el("div","",sw?"A service worker is controlling this page; reloading unregisters it.":"No service worker is controlling this page."));
card.appendChild(det);
box.appendChild(card);
document.body.appendChild(box)}
window.setTimeout(paint,MS)})();`;
}
