import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../../mocks/server';
import AwsCredentialsSetupPage from '../../../src/app/admin/aws-credentials/page';

const mockFields = [
  {
    key: 'profile',
    label: 'AWS profile name',
    description: 'The AWS CLI profile name nyxGPT uses for its own AWS API calls.',
    obtain: "Pick any name -- 'nyxgpt' is a fine default.",
    secret: false,
  },
  {
    key: 'region',
    label: 'AWS region',
    description: "Default region for nyxGPT's AWS API calls.",
    obtain: 'https://docs.aws.amazon.com/general/latest/gr/rande.html',
    secret: false,
  },
  {
    key: 'access_key_id',
    label: 'AWS access key ID',
    description: 'Identifies the IAM user/role nyxGPT authenticates as.',
    obtain: 'https://console.aws.amazon.com/iam',
    secret: true,
  },
  {
    key: 'secret_access_key',
    label: 'AWS secret access key',
    description: 'Paired with the access key ID above.',
    obtain: 'Shown once, alongside the access key ID.',
    secret: true,
  },
];

const mockSecretStore = [
  { key: 'provider', label: 'Secret store provider', description: 'Blank for a local deploy.', value: '' },
  { key: 'region', label: 'Secret store region', description: 'AWS region.', value: '' },
  { key: 'ssm_prefix', label: 'SSM parameter prefix', description: 'Path prefix.', value: '/nyxgpt' },
  { key: 'secretsmanager_id', label: 'Secrets Manager secret name', description: 'Secret name.', value: 'nyxgpt' },
];

function mockStatus(overrides: Record<string, unknown> = {}) {
  return {
    fields: mockFields,
    reference: { profile: '', region: '', credentials_source: '' },
    profile_file_status: { set: false, masked_access_key_id: null },
    keychain_status: { set: false, masked_access_key_id: null, available: true },
    secret_store: mockSecretStore,
    ...overrides,
  };
}

describe('AwsCredentialsSetupPage', () => {
  it('renders profile/region fields and the destination choices', async () => {
    server.use(http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())));
    render(<AwsCredentialsSetupPage />);

    expect(await screen.findByText('AWS profile name')).toBeInTheDocument();
    expect(screen.getByText('AWS region')).toBeInTheDocument();
    expect(screen.getByText('AWS CLI profile file')).toBeInTheDocument();
    expect(screen.getByText('OS keychain')).toBeInTheDocument();
    expect(screen.getByText('Already configured elsewhere')).toBeInTheDocument();
  });

  it('shows the key pair inputs for the default profile destination', async () => {
    server.use(http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())));
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    expect(screen.getByText('AWS access key ID')).toBeInTheDocument();
    expect(screen.getByText('AWS secret access key')).toBeInTheDocument();
  });

  it('hides the key pair inputs when "already configured elsewhere" is chosen', async () => {
    server.use(http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())));
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    await userEvent.click(screen.getByRole('radio', { name: /Already configured elsewhere/ }));

    expect(screen.queryByText('AWS access key ID')).not.toBeInTheDocument();
    expect(screen.queryByText('AWS secret access key')).not.toBeInTheDocument();
  });

  it('shows the current storage status for both destinations', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () =>
        HttpResponse.json(
          mockStatus({
            profile_file_status: { set: true, masked_access_key_id: 'AKIA****MNOP' },
          })
        )
      )
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    expect(screen.getByText('Set (AKIA****MNOP)')).toBeInTheDocument();
  });

  it('posts profile/region/destination and the key pair on save', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())),
      http.post('/api/v1/config/aws-credentials', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body).toEqual({
          destination: 'profile',
          profile: 'nyxgpt',
          region: 'us-east-1',
          access_key_id: 'AKIAABCDEFGHIJKLMNOP',
          secret_access_key: 's3cr3txxxxxxxxxxxxxxxxxxxxxxxxxxxx',
        });
        return HttpResponse.json(
          mockStatus({
            reference: { profile: 'nyxgpt', region: 'us-east-1', credentials_source: 'profile' },
            profile_file_status: { set: true, masked_access_key_id: 'AKIA****MNOP' },
          })
        );
      })
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    const profileInput = screen.getByLabelText('AWS profile name');
    await userEvent.clear(profileInput);
    await userEvent.type(profileInput, 'nyxgpt');
    const regionInput = screen.getByLabelText('AWS region');
    await userEvent.clear(regionInput);
    await userEvent.type(regionInput, 'us-east-1');
    await userEvent.type(screen.getByLabelText('AWS access key ID'), 'AKIAABCDEFGHIJKLMNOP');
    await userEvent.type(
      screen.getByLabelText('AWS secret access key'),
      's3cr3txxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    );

    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(screen.getByText('Saved.')).toBeInTheDocument());
  });

  it('omits the key pair from the save payload for the ambient destination', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())),
      http.post('/api/v1/config/aws-credentials', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body).toEqual({ destination: 'ambient', profile: 'nyxgpt', region: 'us-east-1' });
        return HttpResponse.json(
          mockStatus({
            reference: { profile: 'nyxgpt', region: 'us-east-1', credentials_source: 'ambient' },
          })
        );
      })
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    await userEvent.click(screen.getByRole('radio', { name: /Already configured elsewhere/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(screen.getByText('Saved.')).toBeInTheDocument());
  });

  it('shows a validation error inline on a failed save', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())),
      http.post('/api/v1/config/aws-credentials', () =>
        HttpResponse.json({ detail: "doesn't look like an AWS region" }, { status: 422 })
      )
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    await userEvent.click(screen.getByRole('radio', { name: /Already configured elsewhere/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText("doesn't look like an AWS region")).toBeInTheDocument();
  });

  it('renders and saves the secret store reference fields', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())),
      http.post('/api/v1/config/aws-credentials/secret-store', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body).toEqual({
          provider: 'ssm',
          region: '',
          ssm_prefix: '/nyxgpt',
          secretsmanager_id: 'nyxgpt',
        });
        return HttpResponse.json({
          secret_store: mockSecretStore.map((e) => (e.key === 'provider' ? { ...e, value: 'ssm' } : e)),
        });
      })
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('Secret store provider');
    const providerInput = screen.getByLabelText('Secret store provider');
    await userEvent.type(providerInput, 'ssm');
    await userEvent.click(screen.getByRole('button', { name: 'Save secret store reference' }));

    await waitFor(() =>
      expect(screen.getByText('Secret store reference saved.')).toBeInTheDocument()
    );
  });

  it('links back to the admin dashboard like the other admin pages', async () => {
    server.use(http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())));
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    expect(screen.getByRole('link', { name: /Back to Admin Dashboard/ })).toHaveAttribute(
      'href',
      '/admin/dashboard'
    );
  });

  it('shows a page-level error when the initial status fetch fails', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () =>
        HttpResponse.json({ error: 'config backend unavailable' }, { status: 502 })
      )
    );
    render(<AwsCredentialsSetupPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent('config backend unavailable');
  });

  it('renders a non-Error rejection as text', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (() =>
      Promise.reject('network stack unavailable')) as unknown as typeof fetch;
    try {
      render(<AwsCredentialsSetupPage />);
      expect(await screen.findByRole('alert')).toHaveTextContent('network stack unavailable');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  it('seeds the region with the CLI default when the reference is blank', async () => {
    server.use(http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())));
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    expect(screen.getByLabelText('AWS region')).toHaveValue('us-east-1');
    expect(screen.getByLabelText('AWS profile name')).toHaveValue('nyxgpt');
  });

  it('preselects the saved profile, region, and destination from the stored reference', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () =>
        HttpResponse.json(
          mockStatus({
            reference: { profile: 'acme', region: 'eu-west-1', credentials_source: 'keychain' },
            keychain_status: { set: true, masked_access_key_id: 'AKIA****WXYZ', available: true },
          })
        )
      )
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    expect(screen.getByLabelText('AWS profile name')).toHaveValue('acme');
    expect(screen.getByLabelText('AWS region')).toHaveValue('eu-west-1');
    expect(screen.getByRole('radio', { name: /OS keychain/ })).toBeChecked();
    expect(screen.getByText('Set (AKIA****WXYZ)')).toBeInTheDocument();
  });

  it('reports when the OS keychain backend is unavailable', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () =>
        HttpResponse.json(
          mockStatus({
            keychain_status: { set: false, masked_access_key_id: null, available: false },
          })
        )
      )
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('AWS profile name');
    expect(screen.getByText('keyring not installed')).toBeInTheDocument();
  });

  it('shows an inline error when the secret store reference fails to save', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())),
      http.post('/api/v1/config/aws-credentials/secret-store', () =>
        HttpResponse.json({}, { status: 500 })
      )
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('Secret store provider');
    await userEvent.click(screen.getByRole('button', { name: 'Save secret store reference' }));

    expect(await screen.findByText('HTTP 500')).toBeInTheDocument();
  });

  it('renders secret store fields the server returns that the local drafts do not know about', async () => {
    server.use(
      http.get('/api/v1/config/aws-credentials', () => HttpResponse.json(mockStatus())),
      http.post('/api/v1/config/aws-credentials/secret-store', () =>
        HttpResponse.json({
          secret_store: [
            ...mockSecretStore,
            {
              key: 'vault_path',
              label: 'Vault path',
              description: 'Added by a newer backend.',
              value: '/secret/nyxgpt',
            },
          ],
        })
      )
    );
    render(<AwsCredentialsSetupPage />);

    await screen.findByText('Secret store provider');
    await userEvent.click(screen.getByRole('button', { name: 'Save secret store reference' }));

    await waitFor(() => expect(screen.getByLabelText('Vault path')).toBeInTheDocument());
    expect(screen.getByLabelText('Vault path')).toHaveValue('/secret/nyxgpt');
  });
});
