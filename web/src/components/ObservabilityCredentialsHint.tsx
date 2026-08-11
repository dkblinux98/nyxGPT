/**
 * How to obtain the Grafana / GlitchTip admin logins, for every SRE surface
 * that links out to one of those UIs (#3718).
 *
 * Both passwords are ops-managed secrets that are deliberately never exposed
 * over the HTTP API (#3458/#3466), so this dashboard cannot show the values
 * themselves -- it can only say which wrapped command prints them. That
 * command is the point of the issue: before it existed, following one of
 * these links meant `ssh`-ing to the box and `cat`-ing a secret file by
 * hand, which the Operational Command Wrapping requirement forbids.
 */
export default function ObservabilityCredentialsHint({
  services = 'Grafana and GlitchTip',
}: {
  /** Which UIs the surrounding links point at, for the sentence's subject. */
  services?: string;
}) {
  return (
    <p
      style={{
        margin: '0.5rem 0 0',
        fontSize: 12,
        color: 'var(--muted-foreground)',
        lineHeight: 1.5,
      }}
    >
      Need the {services} admin login? Run <code>nyxgpt ops credentials</code> on the machine
      running the stack, or <code>nyxgpt cloud credentials</code> for a cloud deployment (it reads
      them over the wrapped access path). These are ops-managed secrets and are never returned by
      the API, so they can only be printed by those commands.
    </p>
  );
}
